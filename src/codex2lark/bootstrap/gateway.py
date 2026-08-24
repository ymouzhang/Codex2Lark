from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress

from codex2lark.adapters.openai_responses import OpenAIResponsesModel
from codex2lark.capabilities.artifacts.plugin import FeishuArtifactsPlugin
from codex2lark.capabilities.docs.plugin import FeishuDocsPlugin
from codex2lark.capabilities.im.admission import IMAdmissionService
from codex2lark.capabilities.im.attachments import AttachmentService, SafeAttachmentParser
from codex2lark.capabilities.im.channel_adapter import (
    ChannelPort,
    OfficialChannelEventSource,
    create_official_channel,
)
from codex2lark.capabilities.im.context_provider import IMContextProvider
from codex2lark.capabilities.im.live_reader import (
    IMMessageAPI,
    OfficialIMMessageAPI,
    OfficialLiveIMReader,
)
from codex2lark.capabilities.im.membership import (
    BotAddedAdmissionService,
    MembershipTaskHandler,
)
from codex2lark.capabilities.im.plugin import create_plugin as create_im_plugin
from codex2lark.capabilities.im.publisher import IMOutboxPublisher
from codex2lark.capabilities.im.repository import SQLiteIMRepository
from codex2lark.capabilities.im.task_handler import IMMentionTaskHandler, IMResponseTemplates
from codex2lark.core.budgets import BudgetKind, BudgetLimit
from codex2lark.core.models import Identity
from codex2lark.interfaces.application import create_application
from codex2lark.runtime.context import ContextEngine
from codex2lark.runtime.delegation import DelegateAgentTool, MultiAgentCoordinator
from codex2lark.runtime.harness import AgentHarness, ModelProvider
from codex2lark.runtime.multi_agent import MultiAgentSupervisor
from codex2lark.runtime.outbox import OutboxDispatcher
from codex2lark.runtime.plugins import PluginManager
from codex2lark.runtime.resources import ResourceLoader
from codex2lark.runtime.sessions import SessionStore
from codex2lark.runtime.tasks import DurableTaskWorker
from codex2lark.runtime.tools import (
    ApprovalBroker,
    PolicyDecision,
    SemanticTool,
    ToolContext,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
)
from codex2lark.runtime.types import AgentDefinition, ToolCall, ToolDefinition
from codex2lark.storage.agent_store import SQLiteAgentGraphStore
from codex2lark.storage.blobs import EncryptedBlobStore
from codex2lark.storage.crypto import EnvelopeCipher
from codex2lark.storage.database import SQLiteDatabase
from codex2lark.storage.locking import DataDirectoryLock
from codex2lark.storage.runtime_store import RuntimeStore
from codex2lark.storage.session_store import SQLiteSessionStore

from .config import GatewayConfig

logger = logging.getLogger(__name__)


class AllowConfiguredTools(ToolPolicy):
    async def authorize(
        self, definition: ToolDefinition, call: ToolCall, context: ToolContext
    ) -> PolicyDecision:
        del definition, call
        if not all(
            (
                context.tenant_key,
                context.app_id,
                context.actor_id,
                context.identity_ref,
            )
        ):
            return PolicyDecision(False, "trusted Feishu execution bindings are incomplete")
        return PolicyDecision(True, "tool is enabled by the production capability profile")


class DenyUnconfiguredApprovals(ApprovalBroker):
    async def request(
        self, definition: ToolDefinition, call: ToolCall, context: ToolContext
    ) -> bool:
        del definition, call, context
        return False


class V3Gateway:
    def __init__(
        self,
        *,
        database: SQLiteDatabase,
        plugins: PluginManager,
        source: OfficialChannelEventSource,
        tasks: DurableTaskWorker,
        outbox: OutboxDispatcher,
        poll_interval_ms: int,
        clock_ms: Callable[[], int] | None = None,
        data_lock: DataDirectoryLock | None = None,
    ) -> None:
        self._database = database
        self._plugins = plugins
        self._source = source
        self._tasks = tasks
        self._outbox = outbox
        self._poll_interval = poll_interval_ms / 1000
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._data_lock = data_lock
        self._stop = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("V3 gateway is already running")
        if self._data_lock is not None:
            self._data_lock.acquire()
        try:
            await self._database.open()
            try:
                await self._plugins.start()
                try:
                    await self._source.start()
                except BaseException:
                    await self._plugins.stop()
                    raise
            except BaseException:
                await self._database.close()
                raise
        except BaseException:
            if self._data_lock is not None:
                self._data_lock.release()
            raise
        self._stop.clear()
        self._worker = asyncio.create_task(self._run(), name="codex2lark-v3-worker")
        logger.info("V3 gateway ready")

    async def stop(self) -> None:
        worker = self._worker
        if worker is None:
            return
        source_failure: BaseException | None = None
        try:
            try:
                await self._source.stop()
            except BaseException as exc:
                source_failure = exc
            self._stop.set()
            await worker
            await self._drain_once()
        finally:
            try:
                await self._plugins.stop()
            finally:
                try:
                    await self._database.close()
                finally:
                    if self._data_lock is not None:
                        self._data_lock.release()
            self._worker = None
            logger.info("V3 gateway stopped")
        if source_failure is not None:
            raise source_failure

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self._drain_once()
            except Exception:
                logger.exception("V3 gateway worker iteration failed")
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)

    async def _drain_once(self) -> None:
        now_ms = self._clock_ms()
        await self._outbox.run_once(now_ms=now_ms)
        await self._tasks.run_once(now_ms=now_ms)
        await self._outbox.run_once(now_ms=self._clock_ms())


def create_v3_gateway(
    config: GatewayConfig,
    *,
    channel: ChannelPort | None = None,
    model: ModelProvider | None = None,
    im_api: IMMessageAPI | None = None,
) -> V3Gateway:
    database = SQLiteDatabase(config.data_dir / "runtime.db")
    cipher = EnvelopeCipher(config.master_key)
    runtime_store = RuntimeStore(database, cipher)
    sessions: SessionStore = SQLiteSessionStore(database, cipher)
    im_repository = SQLiteIMRepository(database, cipher)
    active_channel = channel or create_official_channel(
        app_id=config.feishu_app_id,
        app_secret=config.feishu_app_secret,
    )
    resource_loader = ResourceLoader.from_package("codex2lark.bundled_resources")
    im_templates = ResourceLoader.load_im_templates("codex2lark.bundled_resources", "zh-CN")

    def bot_open_id() -> str | None:
        value = getattr(active_channel.bot_identity, "open_id", None)
        return value if isinstance(value, str) and value else None

    templates = IMResponseTemplates(
        completed_suffix=im_templates.completed_suffix,
        blocked_suffix=im_templates.blocked_suffix,
        failed_suffix=im_templates.failed_suffix,
        cancelled_suffix=im_templates.cancelled_suffix,
    )
    admission = IMAdmissionService(
        runtime_store,
        im_repository,
        bot_open_id=bot_open_id,
        acknowledgement_text=im_templates.acknowledgement,
    )
    membership_admission = BotAddedAdmissionService(
        runtime_store,
        app_id=config.feishu_app_id,
        received_at_ms=lambda: int(time.time() * 1000),
    )
    source = OfficialChannelEventSource(
        active_channel,
        admission,
        app_id=config.feishu_app_id,
        received_at_ms=lambda: int(time.time() * 1000),
        bot_added_handler=membership_admission,
    )
    api = im_api or OfficialIMMessageAPI(
        app_id=config.feishu_app_id, app_secret=config.feishu_app_secret
    )
    live_context = IMContextProvider(
        OfficialLiveIMReader(api, bot_open_id=bot_open_id),
        im_repository,
        attachments=AttachmentService(
            im_repository,
            active_channel,
            EncryptedBlobStore(config.data_dir / "blobs", cipher),
            SafeAttachmentParser(),
        ),
        clock_ms=lambda: int(time.time() * 1000),
    )
    authoring = create_application()
    docs_plugin = FeishuDocsPlugin(authoring.docs, config.authoring_identity)
    artifacts_plugin = FeishuArtifactsPlugin(authoring.artifacts, config.authoring_identity)
    plugins = PluginManager(
        runtime_api=1,
        allowlist={"feishu-im", "feishu-docs", "feishu-artifacts"},
    )
    plugins.register(create_im_plugin())
    plugins.register(docs_plugin)
    plugins.register(artifacts_plugin)
    business_tools = [*docs_plugin.tools, *artifacts_plugin.tools]
    business_registry = ToolRegistry(business_tools)
    selected_model = model or OpenAIResponsesModel.from_api_key(
        api_key=config.openai_api_key,
        base_url=config.openai_base_url,
    )
    policy = AllowConfiguredTools()
    approvals = DenyUnconfiguredApprovals()
    graph_store = SQLiteAgentGraphStore(database, cipher)
    child_harness = AgentHarness(
        model=selected_model,
        tools=business_registry,
        tool_executor=ToolExecutor(
            business_registry,
            policy,
            approvals,
            runtime_store,
            write_scope_store=graph_store,
        ),
        resources=resource_loader,
        context=ContextEngine(),
        sessions=sessions,
    )
    supervisor = MultiAgentSupervisor(graph_store)
    coordinator = MultiAgentCoordinator(
        supervisor=supervisor,
        store=graph_store,
        child_harness=child_harness,
        sessions=sessions,
        child_tools=business_registry,
        model_profile=config.model,
    )
    delegation = DelegateAgentTool(
        coordinator,
        tuple(tool.definition.tool_id for tool in business_tools),
    )
    enabled_tools: list[SemanticTool] = [*business_tools, delegation]
    registry = ToolRegistry(enabled_tools)
    harness = AgentHarness(
        model=selected_model,
        tools=registry,
        tool_executor=ToolExecutor(registry, policy, approvals, runtime_store),
        resources=resource_loader,
        context=ContextEngine(),
        sessions=sessions,
        controls=runtime_store,
    )
    definition = AgentDefinition(
        agent_id="feishu-group-root",
        version=1,
        instructions="Follow the selected trusted Codex2Lark resource packages.",
        model_profile=config.model,
        tool_ids=tuple(tool.definition.tool_id for tool in enabled_tools),
        resource_packages=("group-agent-core",),
        budget_limits=(
            BudgetLimit(BudgetKind.MODEL_TOKENS, 32_000),
            BudgetLimit(BudgetKind.TOOL_CALLS, 16),
            BudgetLimit(BudgetKind.EXTERNAL_WRITES, 6),
            BudgetLimit(BudgetKind.AGENT_NODES, 8),
        ),
        max_turns=8,
        max_context_tokens=32_000,
    )
    handler = IMMentionTaskHandler(
        context=live_context,
        harness=harness,
        sessions=sessions,
        definition=definition,
        templates=templates,
        identity_ref=f"bot:{config.feishu_app_id}",
        graph_lifecycle=coordinator,
    )
    task_worker = DurableTaskWorker(
        runtime_store,
        {
            "im.handle_mention": handler,
            "im.ensure_owner_membership": MembershipTaskHandler(
                authoring.membership,
                bot_identity=Identity.BOT,
            ),
        },
        worker_id="v3-task-worker",
        concurrency=config.task_concurrency,
    )
    outbox = OutboxDispatcher(
        runtime_store,
        {"feishu-im.reply": IMOutboxPublisher(active_channel)},
        worker_id="v3-outbox-worker",
    )
    return V3Gateway(
        database=database,
        plugins=plugins,
        source=source,
        tasks=task_worker,
        outbox=outbox,
        poll_interval_ms=config.poll_interval_ms,
        data_lock=DataDirectoryLock(config.data_dir),
    )
