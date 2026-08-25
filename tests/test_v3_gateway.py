from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex2lark.bootstrap.config import GatewayConfig
from codex2lark.bootstrap.gateway import AllowConfiguredTools, V3Gateway, create_v3_gateway
from codex2lark.capabilities.im.live_reader import WireMessagePage
from codex2lark.core.models import CreateDocumentRequest, EditDocumentRequest, Identity
from codex2lark.runtime.plugins import PluginHealth, PluginManager, PluginManifest
from codex2lark.runtime.tools import ToolContext
from codex2lark.runtime.types import (
    ModelRequest,
    ModelResponse,
    ModelUsage,
    ToolCall,
    ToolDefinition,
    ToolEffect,
)
from codex2lark.storage.crypto import MasterKey


class LifecycleDouble:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.opened = False
        self.closed = False
        self.started = False
        self.stopped = False

    async def open(self) -> None:
        self.opened = True

    async def close(self) -> None:
        self.closed = True

    async def start(self) -> None:
        self.started = True
        if self.fail_start:
            raise RuntimeError("source failed")

    async def stop(self) -> None:
        self.stopped = True


class WorkerDouble:
    def __init__(self) -> None:
        self.calls = 0

    async def run_once(self, **parameters: object) -> object:
        del parameters
        self.calls += 1
        return object()


class BlockingWorkerDouble:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

    async def run_once(self, **parameters: object) -> object:
        del parameters
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        raise AssertionError("unreachable")


class ToggleCapabilityPlugin:
    manifest = PluginManifest("feishu-docs", "1.0.0", 1, ("docs.create",))

    def __init__(self, healthy: bool) -> None:
        self.healthy = healthy

    async def initialize(self) -> None:
        pass

    async def health(self) -> PluginHealth:
        return PluginHealth(self.healthy, None if self.healthy else "upstream unavailable")

    async def stop(self) -> None:
        pass


class NamedTogglePlugin(ToggleCapabilityPlugin):
    def __init__(self, plugin_id: str, capability: str, healthy: bool) -> None:
        super().__init__(healthy)
        self.manifest = PluginManifest(plugin_id, "1.0.0", 1, (capability,))


def gateway(database: LifecycleDouble, source: LifecycleDouble) -> tuple[V3Gateway, WorkerDouble]:
    tasks = WorkerDouble()
    outbox = WorkerDouble()
    value = V3Gateway(
        database=database,  # type: ignore[arg-type]
        plugins=LifecycleDouble(),  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        tasks=tasks,  # type: ignore[arg-type]
        outbox=outbox,  # type: ignore[arg-type]
        poll_interval_ms=10,
        clock_ms=lambda: 100,
    )
    return value, tasks


def test_gateway_config_requires_explicit_secrets_and_resolves_state_path(tmp_path) -> None:
    encoded = base64.b64encode(b"k" * 32).decode()
    values = {
        "CODEX2LARK_FEISHU_APP_ID": "cli_app",
        "CODEX2LARK_FEISHU_APP_SECRET": "secret",
        "OPENAI_API_KEY": "model-secret",
        "CODEX2LARK_MODEL": "configured-model",
        "CODEX2LARK_MODEL_INPUT_COST_MICROS_PER_MILLION_TOKENS": "1250000",
        "CODEX2LARK_MODEL_OUTPUT_COST_MICROS_PER_MILLION_TOKENS": "10000000",
        "CODEX2LARK_MASTER_KEY_ID": "key-v1",
        "CODEX2LARK_MASTER_KEY_BASE64": encoded,
        "CODEX2LARK_DATA_DIR": str(tmp_path),
        "CODEX2LARK_STORAGE_MAX_BYTES": "123456",
        "CODEX2LARK_STORAGE_MIN_FREE_BYTES": "789",
        "CODEX2LARK_MAX_ATTACHMENT_BYTES": "456",
    }
    config = GatewayConfig.from_environment(values)

    assert config.data_dir == tmp_path
    assert config.master_key.key == b"k" * 32
    assert config.storage_capacity.maximum_managed_bytes == 123456
    assert config.storage_capacity.minimum_free_bytes == 789
    assert config.max_attachment_bytes == 456
    assert config.enabled_chat_ids == frozenset()
    assert config.authorized_actor_ids == frozenset()
    assert config.run_wall_time_ms == 900_000
    assert config.run_cost_limit_micros == 1_000_000
    assert config.shutdown_drain_ms == 30_000
    assert config.task_limits().group_limit == 2
    assert config.model_provider_concurrency == 4
    assert config.plugin_concurrency == 4
    assert "secret" not in repr(config)
    missing_price = dict(values)
    del missing_price["CODEX2LARK_MODEL_INPUT_COST_MICROS_PER_MILLION_TOKENS"]
    with pytest.raises(ValueError, match="MODEL_INPUT_COST"):
        GatewayConfig.from_environment(missing_price)
    invalid_limits = dict(values)
    invalid_limits["CODEX2LARK_GROUP_CONCURRENCY"] = "5"
    with pytest.raises(ValueError, match="group <= app <= tenant <= global"):
        GatewayConfig.from_environment(invalid_limits)
    (tmp_path / "key-rotation.json").write_text("{}")
    with pytest.raises(ValueError, match="incomplete key rotation"):
        GatewayConfig.from_environment(values)
    with pytest.raises(ValueError, match="CODEX2LARK_FEISHU_APP_ID"):
        GatewayConfig.from_environment({})

    (tmp_path / "key-rotation.json").unlink()
    values.update(
        {
            "CODEX2LARK_CANARY_AGENT_VERSION": "2",
            "CODEX2LARK_CANARY_PERCENT": "5",
            "CODEX2LARK_ROLLOUT_SALT": "test-salt",
            "CODEX2LARK_CANARY_MODEL": "candidate-model",
        }
    )
    canary = GatewayConfig.from_environment(values)
    assert canary.canary_agent_version == 2
    assert canary.canary_percent == 5
    assert canary.canary_model == "candidate-model"
    values["CODEX2LARK_ROLLOUT_SALT"] = ""
    with pytest.raises(ValueError, match="enabled canary"):
        GatewayConfig.from_environment(values)

    values["CODEX2LARK_ROLLOUT_SALT"] = "test-salt"
    values["CODEX2LARK_ENABLED_CHAT_IDS"] = "oc_one,oc_two"
    values["CODEX2LARK_AUTHORIZED_ACTOR_IDS"] = "ou_one,ou_two"
    restricted = GatewayConfig.from_environment(values)
    assert restricted.enabled_chat_ids == frozenset({"oc_one", "oc_two"})
    assert restricted.authorized_actor_ids == frozenset({"ou_one", "ou_two"})
    values["CODEX2LARK_ENABLED_CHAT_IDS"] = "oc_one,,oc_two"
    with pytest.raises(ValueError, match="empty ID"):
        GatewayConfig.from_environment(values)


async def test_v3_gateway_runs_workers_and_drains_on_stop() -> None:
    database = LifecycleDouble()
    source = LifecycleDouble()
    service, tasks = gateway(database, source)

    await service.start()
    await asyncio.sleep(0.02)
    await service.stop()

    assert database.opened and database.closed
    assert source.started and source.stopped
    assert tasks.calls >= 2


async def test_v3_gateway_cancels_active_batch_after_drain_deadline() -> None:
    database = LifecycleDouble()
    source = LifecycleDouble()
    tasks = BlockingWorkerDouble()
    service = V3Gateway(
        database=database,  # type: ignore[arg-type]
        plugins=LifecycleDouble(),  # type: ignore[arg-type]
        source=source,  # type: ignore[arg-type]
        tasks=tasks,  # type: ignore[arg-type]
        outbox=WorkerDouble(),  # type: ignore[arg-type]
        poll_interval_ms=10,
        shutdown_drain_ms=5,
    )

    await service.start()
    await tasks.started.wait()
    await service.stop()

    assert tasks.cancelled.is_set()
    assert database.closed and source.stopped


async def test_v3_gateway_closes_database_when_source_start_fails() -> None:
    database = LifecycleDouble()
    service, _tasks = gateway(database, LifecycleDouble(fail_start=True))

    with pytest.raises(RuntimeError, match="source failed"):
        await service.start()

    assert database.closed is True


async def test_tool_policy_isolates_unhealthy_plugin_and_allows_live_recovery() -> None:
    plugin = ToggleCapabilityPlugin(False)
    manager = PluginManager(
        runtime_api=1,
        allowlist={"feishu-docs"},
        mandatory_plugin_ids=set(),
    )
    manager.register(plugin)
    await manager.start()
    policy = AllowConfiguredTools(manager, {"feishu.docs.create": "feishu-docs"})
    definition = ToolDefinition(
        "feishu.docs.create", 1, "create", {"type": "object"}, ToolEffect.WRITE
    )
    context = ToolContext("run", "/root", "tenant", "app", "user", "session", "bot", 1)

    denied = await policy.authorize(definition, ToolCall("call-1", definition.tool_id, {}), context)
    plugin.healthy = True
    allowed = await policy.authorize(
        definition, ToolCall("call-2", definition.tool_id, {}), context
    )

    assert not denied.allowed and "unhealthy" in denied.reason
    assert allowed.allowed


async def test_tool_policy_checks_owner_and_explicit_drive_dependency() -> None:
    sheets = NamedTogglePlugin("feishu-sheets", "sheets.range.write", True)
    drive = NamedTogglePlugin("feishu-drive", "drive.managed-folder", False)
    manager = PluginManager(
        runtime_api=1,
        allowlist={"feishu-sheets", "feishu-drive"},
        mandatory_plugin_ids=set(),
    )
    manager.register(sheets)
    manager.register(drive)
    await manager.start()
    definition = ToolDefinition(
        "feishu.sheets.create", 1, "create", {"type": "object"}, ToolEffect.WRITE
    )
    policy = AllowConfiguredTools(
        manager,
        {definition.tool_id: ("feishu-sheets", "feishu-drive")},
    )
    context = ToolContext("run", "/root", "tenant", "app", "user", "session", "bot", 1)

    denied = await policy.authorize(definition, ToolCall("call-1", definition.tool_id, {}), context)
    drive.healthy = True
    recovered = await policy.authorize(
        definition, ToolCall("call-2", definition.tool_id, {}), context
    )
    sheets.healthy = False
    owner_failed = await policy.authorize(
        definition, ToolCall("call-3", definition.tool_id, {}), context
    )

    assert not denied.allowed and "feishu-drive" in denied.reason
    assert recovered.allowed
    assert not owner_failed.allowed and "feishu-sheets" in owner_failed.reason


class FakeChannel:
    def __init__(self) -> None:
        self.bot_identity: object | None = None
        self.handlers: dict[str, Any] = {}
        self.sent: list[dict[str, object]] = []
        self.completed = asyncio.Event()

    def on(self, event: str, handler: Any) -> object:
        self.handlers[event] = handler
        return object()

    async def connect_until_ready(self, *, timeout: float | None = 30.0) -> None:
        del timeout
        self.bot_identity = SimpleNamespace(open_id="ou_bot")

    async def disconnect(self) -> None:
        return None

    async def send(self, to: str, message: dict[str, str], opts: dict[str, object]) -> object:
        self.sent.append({"to": to, "message": message, "opts": opts})
        if len(self.sent) >= 2:
            self.completed.set()
        return SimpleNamespace(success=True, message_id=f"om_reply_{len(self.sent)}")

    async def emit_mention(
        self,
        *,
        event_id: str = "event-e2e",
        message_id: str = "om_trigger",
        text: str = "summarize this",
        root_id: str | None = None,
    ) -> None:
        value = SimpleNamespace(
            raw={
                "header": {"event_id": event_id, "tenant_key": "tenant-1"},
                "event": {
                    "sender": {"sender_type": "user"},
                    "message": {
                        "content": json.dumps({"text": f"@_user_1 {text}"}),
                        "message_type": "text",
                        "mentions": [
                            {
                                "key": "@_user_1",
                                "id": {"open_id": "ou_bot"},
                                "name": "Agent",
                            }
                        ],
                        "root_id": root_id,
                    },
                },
            },
            sender_id="ou_user",
            sender=SimpleNamespace(is_bot=False),
            sender_name="Aaron",
            chat_id="oc_group",
            chat_type="group",
            message_id=message_id,
            raw_content_type="text",
            safe_content_text=text,
            content_text=text,
            create_time="100",
            conversation=SimpleNamespace(chat_type="group", thread_id=None),
            resources=[],
        )
        await self.handlers["message"](value)


class E2EMessageAPI:
    def __init__(self, text: str = "summarize this") -> None:
        self.text = text

    async def get(self, message_id: str) -> object:
        return SimpleNamespace(
            message_id=message_id,
            chat_id="oc_group",
            msg_type="text",
            create_time=100,
            update_time=101,
            deleted=False,
            sender=SimpleNamespace(id="ou_user", sender_type="user", tenant_key="tenant-1"),
            body=SimpleNamespace(content=json.dumps({"text": f"@_user_1 {self.text}"})),
            mentions=(SimpleNamespace(id="ou_bot", name="Agent", key="@_user_1"),),
            thread_id=None,
            root_id=None,
            parent_id=None,
        )

    async def list(self, **parameters: object) -> WireMessagePage:
        del parameters
        return WireMessagePage((), False)


class E2EModel:
    async def complete(self, request: ModelRequest) -> ModelResponse:
        assert request.messages[-1].content == "summarize this"
        assert "feishu.chat.digest.publish" in {definition.tool_id for definition in request.tools}
        assert request.remaining_budget["wall_time_ms"] <= 900_000
        assert request.remaining_budget["cost_micros"] == 1_000_000
        return ModelResponse("已经为你整理好摘要。", usage=ModelUsage(10, 8))


class BlockingE2EModel:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        del request
        self.started.set()
        await self.release.wait()
        return ModelResponse("This answer must be cancelled.")


class CanaryE2EModel:
    def __init__(self) -> None:
        self.model_profiles: list[str] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.model_profiles.append(request.model_profile)
        return ModelResponse("Canary request completed.")


class ProfessionalDocumentService:
    def __init__(self) -> None:
        self.created: list[CreateDocumentRequest] = []

    async def search(self, request: object) -> dict[str, object]:
        del request
        return {"ok": True, "scope": "managed_folder", "matches": []}

    async def inspect(self, request: object) -> dict[str, object]:
        del request
        return {
            "ok": True,
            "resource": {"url": "https://example.feishu.cn/docx/professional_v3"},
            "data": {"content": "verified live document"},
            "revision": 1,
        }

    async def create(self, request: object) -> dict[str, object]:
        assert isinstance(request, CreateDocumentRequest)
        assert request.identity is Identity.USER
        assert "<table>" in request.content and "</table>" in request.content
        assert '<whiteboard type="mermaid">' in request.content
        assert "flowchart LR" in request.content
        self.created.append(request)
        return {
            "ok": True,
            "resource": {
                "title": request.title,
                "url": "https://example.feishu.cn/docx/professional_v3",
            },
            "managed_folder": {"name": "Codex2Lark", "token": "fld_managed"},
            "verification": {"status": "passed", "checks": ["live_read_back"]},
        }

    async def edit(self, request: object) -> dict[str, object]:
        del request
        raise AssertionError("professional-document fixture must create, not edit")


class ExistingDocumentService:
    url = "https://example.feishu.cn/docx/existing_architecture"

    def __init__(self) -> None:
        self.search_count = 0
        self.inspect_count = 0
        self.edits: list[EditDocumentRequest] = []

    async def search(self, request: object) -> dict[str, object]:
        del request
        self.search_count += 1
        return {
            "ok": True,
            "scope": "managed_folder",
            "matches": [{"title": "Existing Architecture", "url": self.url}],
        }

    async def inspect(self, request: object) -> dict[str, object]:
        del request
        self.inspect_count += 1
        return {
            "ok": True,
            "resource": {"document_id": "existing_architecture", "url": self.url},
            "data": {"content": "<p>Current architecture.</p>"},
            "revision": 7,
        }

    async def create(self, request: object) -> dict[str, object]:
        del request
        raise AssertionError("existing-document fixture must edit, not create")

    async def edit(self, request: object) -> dict[str, object]:
        assert isinstance(request, EditDocumentRequest)
        assert request.document_title == "Existing Architecture"
        assert request.operations[0].content == "<p>Added recovery section.</p>"
        self.edits.append(request)
        return {
            "ok": True,
            "resource": {"title": "Existing Architecture", "url": self.url},
            "revision": 8,
            "verification": {"status": "passed"},
        }


class UnusedArtifactService:
    async def render_whiteboard(self, request: object) -> dict[str, object]:
        del request
        raise AssertionError("diagram must be embedded in the document create")

    async def create_workbook(self, request: object) -> dict[str, object]:
        del request
        raise AssertionError("unexpected artifact call")

    async def write_sheet(self, request: object) -> dict[str, object]:
        del request
        raise AssertionError("unexpected artifact call")

    async def create_base(self, request: object) -> dict[str, object]:
        del request
        raise AssertionError("unexpected artifact call")

    async def upsert_base_records(self, request: object) -> dict[str, object]:
        del request
        raise AssertionError("unexpected artifact call")


class UnusedDriveService:
    async def ensure_managed_folder(self, identity: Identity) -> dict[str, object]:
        del identity
        raise AssertionError("unexpected Drive call")

    async def find_managed_folder(self, identity: Identity) -> dict[str, object] | None:
        del identity
        raise AssertionError("unexpected Drive call")


class UnusedMembershipService:
    async def ensure_current_user(
        self, *, chat_id: str, chat_identity: Identity
    ) -> dict[str, object]:
        del chat_id, chat_identity
        return {"ok": True}


class UnusedChatDigestService:
    async def publish(self, request: object) -> dict[str, object]:
        del request
        raise AssertionError("unexpected chat digest call")


class AuthoringFixture:
    def __init__(self, docs: object | None = None) -> None:
        self.drive = UnusedDriveService()
        self.docs = docs or ProfessionalDocumentService()
        artifacts = UnusedArtifactService()
        self.sheets = artifacts
        self.base = artifacts
        self.whiteboard = artifacts
        self.membership = UnusedMembershipService()
        self.chat_digest = UnusedChatDigestService()


class ProfessionalDocumentModel:
    content_marker = "runtime-professional-body-must-not-persist"

    def __init__(self) -> None:
        self.turn = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.turn += 1
        if self.turn == 1:
            assert request.messages[-1].content == "create the professional architecture document"
            return ModelResponse(
                "",
                (
                    ToolCall(
                        "create-professional-doc",
                        "feishu.docs.create",
                        {
                            "title": "Codex2Lark V3 Architecture",
                            "content_xml": (
                                "<h1>Codex2Lark V3 Architecture</h1>"
                                "<table><tr><th>Component</th><th>Responsibility</th></tr>"
                                f"<tr><td>Harness</td><td>{self.content_marker}</td></tr></table>"
                                '<whiteboard type="mermaid">flowchart LR; IM--&gt;Harness; '
                                "Harness--&gt;Docs</whiteboard>"
                            ),
                            "required_text": ["Codex2Lark V3 Architecture", "Harness"],
                        },
                    ),
                ),
            )
        tool_results = [message for message in request.messages if message.tool_call_id]
        assert tool_results and '"state": "verified"' in tool_results[-1].content
        return ModelResponse(
            "《Codex2Lark V3 Architecture》已创建并完成回读验证: "
            "https://example.feishu.cn/docx/professional_v3"
        )


class ExistingDocumentEditModel:
    def __init__(self) -> None:
        self.turn = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.turn += 1
        if self.turn == 1:
            return ModelResponse(
                "",
                (
                    ToolCall(
                        "search-existing",
                        "feishu.docs.search",
                        {"title": "Existing Architecture"},
                    ),
                ),
            )
        if self.turn == 2:
            assert ExistingDocumentService.url in request.messages[-1].content
            return ModelResponse(
                "",
                (
                    ToolCall(
                        "inspect-existing",
                        "feishu.docs.inspect",
                        {"resource": ExistingDocumentService.url},
                    ),
                ),
            )
        if self.turn == 3:
            assert "Current architecture" in request.messages[-1].content
            return ModelResponse(
                "",
                (
                    ToolCall(
                        "edit-existing",
                        "feishu.docs.edit",
                        {
                            "resource": None,
                            "document_title": "Existing Architecture",
                            "command": "append",
                            "content_xml": "<p>Added recovery section.</p>",
                            "pattern": None,
                            "block_id": None,
                            "change_summary": "Add recovery section",
                            "required_text": ["Added recovery section."],
                        },
                    ),
                ),
            )
        assert '"state": "verified"' in request.messages[-1].content
        return ModelResponse(
            "《Existing Architecture》已修改并完成回读验证: " + ExistingDocumentService.url
        )


def e2e_config(path: Path) -> GatewayConfig:
    return GatewayConfig(
        feishu_app_id="app-1",
        feishu_app_secret="secret",
        openai_api_key="model-secret",
        model="configured-model",
        master_key=MasterKey("test", b"k" * 32),
        data_dir=path,
        model_input_cost_micros_per_million_tokens=1_250_000,
        model_output_cost_micros_per_million_tokens=10_000_000,
        poll_interval_ms=10,
    )


async def test_composed_gateway_admits_executes_and_sends_one_terminal_reply(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    service = create_v3_gateway(
        e2e_config(tmp_path),
        channel=channel,  # type: ignore[arg-type]
        model=E2EModel(),
        im_api=E2EMessageAPI(),
    )
    await service.start()
    try:
        await channel.emit_mention()
        await asyncio.wait_for(channel.completed.wait(), timeout=1)
    finally:
        await service.stop()

    assert len(channel.sent) == 3
    assert "开始处理" in channel.sent[1]["message"]["text"]
    assert "认真帮你处理" in str(channel.sent[0]["message"])
    assert "已经为你整理好摘要" in str(channel.sent[2]["message"])
    assert "已经处理完成" in str(channel.sent[2]["message"])
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        graph = connection.execute("SELECT status FROM runtime_graphs").fetchone()
    assert graph == ("completed",)


async def test_group_request_creates_and_verifies_professional_document(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    authoring = AuthoringFixture()
    service = create_v3_gateway(
        e2e_config(tmp_path),
        channel=channel,  # type: ignore[arg-type]
        model=ProfessionalDocumentModel(),
        im_api=E2EMessageAPI("create the professional architecture document"),
        authoring=authoring,  # type: ignore[arg-type]
    )
    await service.start()
    try:
        await channel.emit_mention(text="create the professional architecture document")

        async def terminal_was_sent() -> None:
            while len(channel.sent) < 3:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(terminal_was_sent(), timeout=1)
    finally:
        await service.stop()

    assert len(authoring.docs.created) == 1
    assert len(channel.sent) == 3
    assert "开始处理" in str(channel.sent[1]["message"])
    assert "Codex2Lark V3 Architecture" in str(channel.sent[2]["message"])
    assert "https://example.feishu.cn/docx/professional_v3" in str(channel.sent[2]["message"])
    assert "已经处理完成" in str(channel.sent[2]["message"])
    assert (
        ProfessionalDocumentModel.content_marker.encode()
        not in (tmp_path / "runtime.db").read_bytes()
    )


async def test_canary_definition_is_bound_at_admission_and_used_by_harness(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    model = CanaryE2EModel()
    config = replace(
        e2e_config(tmp_path),
        canary_agent_version=2,
        canary_percent=100,
        rollout_salt="test-salt",
        canary_model="candidate-model",
    )
    service = create_v3_gateway(
        config,
        channel=channel,  # type: ignore[arg-type]
        model=model,
        im_api=E2EMessageAPI(),
        authoring=AuthoringFixture(),  # type: ignore[arg-type]
    )
    await service.start()
    try:
        await channel.emit_mention()

        async def terminal_was_sent() -> None:
            while len(channel.sent) < 3:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(terminal_was_sent(), timeout=1)
    finally:
        await service.stop()

    assert model.model_profiles == ["candidate-model"]
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        graph = connection.execute(
            "SELECT agent_definition_version, status FROM runtime_graphs"
        ).fetchone()
    assert graph == (2, "completed")


async def test_group_request_finds_inspects_edits_and_verifies_existing_document(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    docs = ExistingDocumentService()
    service = create_v3_gateway(
        e2e_config(tmp_path),
        channel=channel,  # type: ignore[arg-type]
        model=ExistingDocumentEditModel(),
        im_api=E2EMessageAPI("update the Existing Architecture document"),
        authoring=AuthoringFixture(docs),  # type: ignore[arg-type]
    )
    await service.start()
    try:
        await channel.emit_mention(text="update the Existing Architecture document")

        async def terminal_was_sent() -> None:
            while len(channel.sent) < 3:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(terminal_was_sent(), timeout=1)
    finally:
        await service.stop()

    assert len(docs.edits) == 1
    assert docs.search_count >= 1
    assert docs.inspect_count >= 1
    assert len(channel.sent) == 3
    assert "Existing Architecture" in str(channel.sent[-1]["message"])
    assert ExistingDocumentService.url in str(channel.sent[-1]["message"])
    assert "已经处理完成" in str(channel.sent[-1]["message"])


async def test_composed_gateway_routes_same_requester_cancel_to_active_root(
    tmp_path: Path,
) -> None:
    channel = FakeChannel()
    model = BlockingE2EModel()
    service = create_v3_gateway(
        e2e_config(tmp_path),
        channel=channel,  # type: ignore[arg-type]
        model=model,
        im_api=E2EMessageAPI(),
    )
    await service.start()
    try:
        await channel.emit_mention()
        await asyncio.wait_for(model.started.wait(), timeout=1)
        await channel.emit_mention(
            event_id="event-cancel",
            message_id="om_cancel",
            text="/cancel",
            root_id="om_trigger",
        )
        model.release.set()

        async def terminal_was_sent() -> None:
            while len(channel.sent) < 4:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(terminal_was_sent(), timeout=1)
    finally:
        model.release.set()
        await service.stop()

    assert len(channel.sent) == 4
    assert "取消" in str(channel.sent[-1]["message"])
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        graph = connection.execute("SELECT status FROM runtime_graphs").fetchone()
        nodes = connection.execute("SELECT DISTINCT status FROM runtime_agent_nodes").fetchall()
        control = connection.execute(
            "SELECT state, target_task_id FROM runtime_run_controls"
        ).fetchone()
    assert graph == ("cancelled",)
    assert nodes == [("cancelled",)]
    assert control is not None and control[0] == "applied"
