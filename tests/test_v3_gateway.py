from __future__ import annotations

import asyncio
import base64
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex2lark.bootstrap.config import GatewayConfig
from codex2lark.bootstrap.gateway import V3Gateway, create_v3_gateway
from codex2lark.capabilities.im.live_reader import WireMessagePage
from codex2lark.runtime.types import ModelRequest, ModelResponse, ModelUsage
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
    config = GatewayConfig.from_environment(
        {
            "CODEX2LARK_FEISHU_APP_ID": "cli_app",
            "CODEX2LARK_FEISHU_APP_SECRET": "secret",
            "OPENAI_API_KEY": "model-secret",
            "CODEX2LARK_MODEL": "configured-model",
            "CODEX2LARK_MASTER_KEY_ID": "key-v1",
            "CODEX2LARK_MASTER_KEY_BASE64": encoded,
            "CODEX2LARK_DATA_DIR": str(tmp_path),
            "CODEX2LARK_STORAGE_MAX_BYTES": "123456",
            "CODEX2LARK_STORAGE_MIN_FREE_BYTES": "789",
            "CODEX2LARK_MAX_ATTACHMENT_BYTES": "456",
        }
    )

    assert config.data_dir == tmp_path
    assert config.master_key.key == b"k" * 32
    assert config.storage_capacity.maximum_managed_bytes == 123456
    assert config.storage_capacity.minimum_free_bytes == 789
    assert config.max_attachment_bytes == 456
    assert "secret" not in repr(config)
    with pytest.raises(ValueError, match="CODEX2LARK_FEISHU_APP_ID"):
        GatewayConfig.from_environment({})


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


async def test_v3_gateway_closes_database_when_source_start_fails() -> None:
    database = LifecycleDouble()
    service, _tasks = gateway(database, LifecycleDouble(fail_start=True))

    with pytest.raises(RuntimeError, match="source failed"):
        await service.start()

    assert database.closed is True


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
    async def get(self, message_id: str) -> object:
        return SimpleNamespace(
            message_id=message_id,
            chat_id="oc_group",
            msg_type="text",
            create_time=100,
            update_time=101,
            deleted=False,
            sender=SimpleNamespace(id="ou_user", sender_type="user", tenant_key="tenant-1"),
            body=SimpleNamespace(content='{"text":"@_user_1 summarize this"}'),
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


def e2e_config(path: Path) -> GatewayConfig:
    return GatewayConfig(
        feishu_app_id="app-1",
        feishu_app_secret="secret",
        openai_api_key="model-secret",
        model="configured-model",
        master_key=MasterKey("test", b"k" * 32),
        data_dir=path,
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

    assert len(channel.sent) == 2
    assert "认真帮你处理" in str(channel.sent[0]["message"])
    assert "已经为你整理好摘要" in str(channel.sent[1]["message"])
    assert "已经处理完成" in str(channel.sent[1]["message"])
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        graph = connection.execute("SELECT status FROM runtime_graphs").fetchone()
    assert graph == ("completed",)


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
            while len(channel.sent) < 3:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(terminal_was_sent(), timeout=1)
    finally:
        model.release.set()
        await service.stop()

    assert len(channel.sent) == 3
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
