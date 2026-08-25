from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from typing import Protocol, TypeVar

from codex2lark.adapters.openai_responses import OpenAIResponsesModel
from codex2lark.capabilities.artifacts.plugin import FeishuArtifactsPlugin
from codex2lark.capabilities.artifacts.tools import ArtifactService
from codex2lark.capabilities.chat_digest.plugin import FeishuChatDigestPlugin
from codex2lark.capabilities.chat_digest.tools import ChatDigestService
from codex2lark.capabilities.docs.plugin import FeishuDocsPlugin
from codex2lark.capabilities.docs.tools import DocumentService
from codex2lark.capabilities.im.admission import IMAdmissionService
from codex2lark.capabilities.im.admission_policy import IMAdmissionPolicy
from codex2lark.capabilities.im.attachments import AttachmentService, SafeAttachmentParser
from codex2lark.capabilities.im.channel_adapter import (
    ChannelPort,
    EventSourceHealth,
    OfficialChannelEventSource,
    create_official_channel,
)
from codex2lark.capabilities.im.context_provider import IMContextProvider
from codex2lark.capabilities.im.lifecycle import (
    IMLifecycleAdmissionService,
    IMLifecycleTaskHandler,
)
from codex2lark.capabilities.im.live_reader import (
    IMMessageAPI,
    OfficialIMMessageAPI,
    OfficialLiveIMReader,
)
from codex2lark.capabilities.im.membership import (
    BotAddedAdmissionService,
    MembershipService,
    MembershipTaskHandler,
)
from codex2lark.capabilities.im.plugin import create_plugin as create_im_plugin
from codex2lark.capabilities.im.publisher import IMOutboxPublisher
from codex2lark.capabilities.im.repository import SQLiteIMRepository
from codex2lark.capabilities.im.task_handler import IMMentionTaskHandler, IMResponseTemplates
from codex2lark.core.budgets import BudgetKind, BudgetLimit
from codex2lark.core.models import Identity
from codex2lark.interfaces.application import create_application
from codex2lark.runtime.approvals import ApprovalDecisionService, DurableApprovalBroker
from codex2lark.runtime.capacity import FairCapacityGate
from codex2lark.runtime.context import ContextEngine
from codex2lark.runtime.delegation import (
    AgentMessageTool,
    AgentStatusTool,
    DelegateAgentTool,
    MultiAgentCoordinator,
)
from codex2lark.runtime.harness import AgentHarness, ModelProvider
from codex2lark.runtime.multi_agent import MultiAgentSupervisor
from codex2lark.runtime.outbox import OutboxDispatcher
from codex2lark.runtime.plugins import PluginManager
from codex2lark.runtime.resources import ResourceLoader
from codex2lark.runtime.rollout import RootAgentRollout
from codex2lark.runtime.sessions import SessionStore
from codex2lark.runtime.tasks import DurableTaskWorker
from codex2lark.runtime.tools import (
    PolicyDecision,
    SemanticTool,
    ToolContext,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
)
from codex2lark.runtime.types import AgentDefinition, ToolCall, ToolDefinition, ToolEffect
from codex2lark.storage.agent_store import SQLiteAgentGraphStore
from codex2lark.storage.blobs import EncryptedBlobStore
from codex2lark.storage.capacity import StorageCapacityMonitor
from codex2lark.storage.crypto import EnvelopeCipher
from codex2lark.storage.database import SQLiteDatabase
from codex2lark.storage.locking import DataDirectoryLock
from codex2lark.storage.runtime_store import RuntimeStore
from codex2lark.storage.session_store import SQLiteSessionStore

from .config import GatewayConfig

logger = logging.getLogger(__name__)
_T = TypeVar("_T")


class AuthoringServices(Protocol):
    @property
    def docs(self) -> DocumentService: ...

    @property
    def artifacts(self) -> ArtifactService: ...

    @property
    def membership(self) -> MembershipService: ...

    @property
    def chat_digest(self) -> ChatDigestService: ...


class AllowConfiguredTools(ToolPolicy):
    def __init__(
        self,
        plugins: PluginManager | None = None,
        tool_plugin_ids: dict[str, str] | None = None,
    ) -> None:
        self._plugins = plugins
        self._tool_plugin_ids = dict(tool_plugin_ids or {})

    async def authorize(
        self, definition: ToolDefinition, call: ToolCall, context: ToolContext
    ) -> PolicyDecision:
        del call
        plugin_id = self._tool_plugin_ids.get(definition.tool_id)
        if plugin_id is not None and self._plugins is not None:
            health = await self._plugins.current_health(plugin_id)
            if not health.healthy:
                return PolicyDecision(
                    False,
                    f"capability plugin is unhealthy: {plugin_id}",
                )
        if not all(
            (
                context.tenant_key,
                context.app_id,
                context.actor_id,
                context.identity_ref,
            )
        ):
            return PolicyDecision(False, "trusted Feishu execution bindings are incomplete")
        return PolicyDecision(
            True,
            "tool is enabled by the production capability profile",
            approval_required=definition.effect is ToolEffect.DESTRUCTIVE,
        )


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
        shutdown_drain_ms: int = 30_000,
        clock_ms: Callable[[], int] | None = None,
        data_lock: DataDirectoryLock | None = None,
    ) -> None:
        self._database = database
        self._plugins = plugins
        self._source = source
        self._tasks = tasks
        self._outbox = outbox
        self._poll_interval = poll_interval_ms / 1000
        if shutdown_drain_ms < 1:
            raise ValueError("Gateway shutdown drain must be positive")
        self._shutdown_timeout = shutdown_drain_ms / 1000
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
        lifecycle_failure: BaseException | None = None
        self._stop.set()
        try:
            try:
                await self._bounded(self._source.stop())
            except BaseException as exc:
                lifecycle_failure = exc
            try:
                await self._bounded(worker)
            except TimeoutError:
                worker.cancel()
                with suppress(asyncio.CancelledError, TimeoutError):
                    await self._bounded(worker)
                logger.warning("V3 gateway drain deadline expired; active tasks were cancelled")
            try:
                await self._bounded(self._outbox.run_once(now_ms=self._clock_ms()))
            except BaseException as exc:
                lifecycle_failure = lifecycle_failure or exc
        finally:
            try:
                await self._bounded(self._plugins.stop())
            except BaseException as exc:
                lifecycle_failure = lifecycle_failure or exc
            finally:
                try:
                    await self._bounded(self._database.close())
                except BaseException as exc:
                    lifecycle_failure = lifecycle_failure or exc
                finally:
                    if self._data_lock is not None:
                        self._data_lock.release()
            self._worker = None
            logger.info("V3 gateway stopped")
        if lifecycle_failure is not None:
            raise lifecycle_failure

    async def _bounded(self, awaitable: Awaitable[_T]) -> _T:
        async with asyncio.timeout(self._shutdown_timeout):
            return await awaitable

    def source_health(self) -> EventSourceHealth:
        return self._source.health()

    async def wait_source_health_change(self, after_version: int) -> EventSourceHealth:
        return await self._source.wait_health_change(after_version)

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
    authoring: AuthoringServices | None = None,
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
    rollout = RootAgentRollout(
        stable_version=1,
        canary_version=config.canary_agent_version,
        canary_percent=config.canary_percent,
        salt=config.rollout_salt,
    )

    def bot_open_id() -> str | None:
        value = getattr(active_channel.bot_identity, "open_id", None)
        return value if isinstance(value, str) and value else None

    templates = IMResponseTemplates(
        progress_started=im_templates.progress_started,
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
        agent_definition_version=lambda message: rollout.select(
            message.tenant_key, message.app_id, message.chat_id
        ),
        policy=IMAdmissionPolicy(
            enabled_chat_ids=config.enabled_chat_ids,
            authorized_actor_ids=config.authorized_actor_ids,
        ),
    )
    membership_admission = BotAddedAdmissionService(
        runtime_store,
        app_id=config.feishu_app_id,
        received_at_ms=lambda: int(time.time() * 1000),
    )
    lifecycle_admission = IMLifecycleAdmissionService(
        runtime_store,
        app_id=config.feishu_app_id,
        received_at_ms=lambda: int(time.time() * 1000),
    )
    approval_decisions = ApprovalDecisionService(
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
        lifecycle_handler=lifecycle_admission,
        card_action_handler=approval_decisions,
    )
    api = im_api or OfficialIMMessageAPI(
        app_id=config.feishu_app_id, app_secret=config.feishu_app_secret
    )
    blob_store = EncryptedBlobStore(config.data_dir / "blobs", cipher)
    capacity = StorageCapacityMonitor(config.data_dir, config.storage_capacity)
    live_context = IMContextProvider(
        OfficialLiveIMReader(api, bot_open_id=bot_open_id),
        im_repository,
        attachments=AttachmentService(
            im_repository,
            active_channel,
            blob_store,
            SafeAttachmentParser(),
            max_attachment_bytes=config.max_attachment_bytes,
            capacity=capacity,
        ),
        clock_ms=lambda: int(time.time() * 1000),
    )
    active_authoring = authoring or create_application()
    docs_plugin = FeishuDocsPlugin(active_authoring.docs, config.authoring_identity)
    artifacts_plugin = FeishuArtifactsPlugin(active_authoring.artifacts, config.authoring_identity)
    chat_digest_plugin = FeishuChatDigestPlugin(
        active_authoring.chat_digest, config.authoring_identity
    )
    plugins = PluginManager(
        runtime_api=1,
        allowlist={
            "feishu-im",
            "feishu-docs",
            "feishu-artifacts",
            "feishu-chat-digest",
        },
        mandatory_plugin_ids={"feishu-im"},
    )
    plugins.register(create_im_plugin())
    plugins.register(docs_plugin)
    plugins.register(artifacts_plugin)
    plugins.register(chat_digest_plugin)
    business_tools = [
        *docs_plugin.tools,
        *artifacts_plugin.tools,
        *chat_digest_plugin.tools,
    ]
    business_registry = ToolRegistry(business_tools)
    selected_model = model or OpenAIResponsesModel.from_api_key(
        api_key=config.openai_api_key,
        input_cost_micros_per_million_tokens=(config.model_input_cost_micros_per_million_tokens),
        output_cost_micros_per_million_tokens=(config.model_output_cost_micros_per_million_tokens),
        base_url=config.openai_base_url,
    )
    tool_plugin_ids = {
        tool.definition.tool_id: plugin.manifest.plugin_id
        for plugin in (docs_plugin, artifacts_plugin, chat_digest_plugin)
        for tool in plugin.tools
    }
    policy = AllowConfiguredTools(plugins, tool_plugin_ids)
    approvals = DurableApprovalBroker(runtime_store)
    graph_store = SQLiteAgentGraphStore(database, cipher)
    capacity_gate = FairCapacityGate()
    child_harness = AgentHarness(
        model=selected_model,
        tools=business_registry,
        tool_executor=ToolExecutor(
            business_registry,
            policy,
            approvals,
            runtime_store,
            write_scope_store=graph_store,
            capacity_gate=capacity_gate,
            tool_plugin_ids=tool_plugin_ids,
            plugin_concurrency=config.plugin_concurrency,
        ),
        resources=resource_loader,
        context=ContextEngine(),
        sessions=sessions,
        capacity_gate=capacity_gate,
        provider_id="openai-responses",
        provider_concurrency=config.model_provider_concurrency,
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
    enabled_tools: list[SemanticTool] = [
        *business_tools,
        delegation,
        AgentMessageTool(coordinator),
        AgentStatusTool(coordinator),
    ]
    registry = ToolRegistry(enabled_tools)
    harness = AgentHarness(
        model=selected_model,
        tools=registry,
        tool_executor=ToolExecutor(
            registry,
            policy,
            approvals,
            runtime_store,
            write_scope_store=graph_store,
            capacity_gate=capacity_gate,
            tool_plugin_ids=tool_plugin_ids,
            plugin_concurrency=config.plugin_concurrency,
        ),
        resources=resource_loader,
        context=ContextEngine(),
        sessions=sessions,
        controls=runtime_store,
        capacity_gate=capacity_gate,
        provider_id="openai-responses",
        provider_concurrency=config.model_provider_concurrency,
    )

    def root_definition(version: int, model_profile: str) -> AgentDefinition:
        return AgentDefinition(
            agent_id="feishu-group-root",
            version=version,
            instructions="Follow the selected trusted Codex2Lark resource packages.",
            model_profile=model_profile,
            tool_ids=tuple(tool.definition.tool_id for tool in enabled_tools),
            resource_packages=("group-agent-core",),
            budget_limits=(
                BudgetLimit(BudgetKind.MODEL_TOKENS, 32_000),
                BudgetLimit(BudgetKind.TOOL_CALLS, 16),
                BudgetLimit(BudgetKind.EXTERNAL_WRITES, 6),
                BudgetLimit(BudgetKind.AGENT_NODES, 8),
                BudgetLimit(BudgetKind.WALL_TIME_MS, config.run_wall_time_ms),
                BudgetLimit(BudgetKind.COST_MICROS, config.run_cost_limit_micros),
            ),
            max_turns=8,
            max_context_tokens=32_000,
        )

    definition = root_definition(1, config.model)
    definitions = {definition.version: definition}
    if config.canary_agent_version is not None:
        canary = root_definition(
            config.canary_agent_version,
            config.canary_model or config.model,
        )
        definitions[canary.version] = canary
    handler = IMMentionTaskHandler(
        context=live_context,
        harness=harness,
        sessions=sessions,
        definition=definition,
        templates=templates,
        identity_ref=f"bot:{config.feishu_app_id}",
        task_outbox=runtime_store,
        graph_lifecycle=coordinator,
        definitions=definitions,
    )
    task_worker = DurableTaskWorker(
        runtime_store,
        {
            "im.handle_mention": handler,
            "im.ensure_owner_membership": MembershipTaskHandler(
                active_authoring.membership,
                bot_identity=Identity.BOT,
                access_repository=im_repository,
            ),
            "im.invalidate_message": IMLifecycleTaskHandler(im_repository, blob_store),
            "im.revoke_chat_access": IMLifecycleTaskHandler(im_repository, blob_store),
        },
        worker_id="v3-task-worker",
        concurrency=config.task_concurrency,
        limits=config.task_limits(),
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
        shutdown_drain_ms=config.shutdown_drain_ms,
        data_lock=DataDirectoryLock(config.data_dir),
    )
