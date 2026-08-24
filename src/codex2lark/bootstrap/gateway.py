from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from contextlib import suppress

from codex2lark.adapters.openai_responses import OpenAIResponsesModel
from codex2lark.capabilities.im.admission import IMAdmissionService
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
from codex2lark.capabilities.im.publisher import IMOutboxPublisher
from codex2lark.capabilities.im.repository import SQLiteIMRepository
from codex2lark.capabilities.im.task_handler import IMMentionTaskHandler, IMResponseTemplates
from codex2lark.core.budgets import BudgetKind, BudgetLimit
from codex2lark.runtime.context import ContextEngine
from codex2lark.runtime.harness import AgentHarness, ModelProvider
from codex2lark.runtime.outbox import OutboxDispatcher
from codex2lark.runtime.resources import ResourceLoader
from codex2lark.runtime.sessions import SessionStore
from codex2lark.runtime.tasks import DurableTaskWorker
from codex2lark.runtime.tools import (
    ApprovalBroker,
    PolicyDecision,
    ToolContext,
    ToolExecutor,
    ToolPolicy,
    ToolRegistry,
)
from codex2lark.runtime.types import AgentDefinition, ToolCall, ToolDefinition
from codex2lark.storage.crypto import EnvelopeCipher
from codex2lark.storage.database import SQLiteDatabase
from codex2lark.storage.runtime_store import RuntimeStore
from codex2lark.storage.session_store import SQLiteSessionStore

from .config import GatewayConfig

logger = logging.getLogger(__name__)


class DenyUnconfiguredTools(ToolPolicy):
    async def authorize(
        self, definition: ToolDefinition, call: ToolCall, context: ToolContext
    ) -> PolicyDecision:
        del definition, call, context
        return PolicyDecision(False, "tool is not enabled by the production capability profile")


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
        source: OfficialChannelEventSource,
        tasks: DurableTaskWorker,
        outbox: OutboxDispatcher,
        poll_interval_ms: int,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self._database = database
        self._source = source
        self._tasks = tasks
        self._outbox = outbox
        self._poll_interval = poll_interval_ms / 1000
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._stop = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._worker is not None:
            raise RuntimeError("V3 gateway is already running")
        await self._database.open()
        try:
            await self._source.start()
        except BaseException:
            await self._database.close()
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
            await self._database.close()
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

    def bot_open_id() -> str | None:
        value = getattr(active_channel.bot_identity, "open_id", None)
        return value if isinstance(value, str) and value else None

    templates = IMResponseTemplates(
        completed_suffix="已经处理完成啦。如果哪里还不清楚，随时问我就好。",  # noqa: RUF001
        blocked_suffix="目前还需要一点信息才能继续。你补充后告诉我，我会接着处理。",  # noqa: RUF001
        failed_suffix="这次没有顺利完成，我已经说明原因。你愿意的话，我们可以一起换个方式继续。",  # noqa: RUF001
        cancelled_suffix="这项处理已经取消。如果想重新开始，再告诉我就好。",  # noqa: RUF001
    )
    admission = IMAdmissionService(
        runtime_store,
        im_repository,
        bot_open_id=bot_open_id,
        acknowledgement_text="收到啦，我会认真帮你处理，完成后马上回来告诉你～",  # noqa: RUF001
    )
    source = OfficialChannelEventSource(
        active_channel,
        admission,
        app_id=config.feishu_app_id,
        received_at_ms=lambda: int(time.time() * 1000),
    )
    api = im_api or OfficialIMMessageAPI(
        app_id=config.feishu_app_id, app_secret=config.feishu_app_secret
    )
    live_context = IMContextProvider(
        OfficialLiveIMReader(api, bot_open_id=bot_open_id),
        im_repository,
    )
    registry = ToolRegistry([])
    harness = AgentHarness(
        model=model
        or OpenAIResponsesModel.from_api_key(
            api_key=config.openai_api_key,
            base_url=config.openai_base_url,
        ),
        tools=registry,
        tool_executor=ToolExecutor(
            registry,
            DenyUnconfiguredTools(),
            DenyUnconfiguredApprovals(),
        ),
        resources=ResourceLoader([]),
        context=ContextEngine(),
        sessions=sessions,
    )
    definition = AgentDefinition(
        agent_id="feishu-group-root",
        version=1,
        instructions=(
            "You are Codex2Lark, a careful Feishu group assistant. Answer the user's "
            "request using only provided evidence and enabled semantic tools. Never claim an "
            "external action completed without verified tool evidence. Be warm, concise, and "
            "state clearly what was or was not completed."
        ),
        model_profile=config.model,
        tool_ids=(),
        budget_limits=(BudgetLimit(BudgetKind.MODEL_TOKENS, 32_000),),
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
    )
    task_worker = DurableTaskWorker(
        runtime_store,
        {"im.handle_mention": handler},
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
        source=source,
        tasks=task_worker,
        outbox=outbox,
        poll_interval_ms=config.poll_interval_ms,
    )
