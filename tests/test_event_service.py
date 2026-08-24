from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import codex2lark.realtime.source as event_module
from codex2lark.adapters.lark_cli import LarkCli
from codex2lark.core.errors import Codex2LarkError
from codex2lark.realtime.delivery import EventReference
from codex2lark.realtime.handlers import BOT_ADDED_EVENT_KEY
from codex2lark.realtime.source import LarkLongConnectionEventSource


class FakePublisher:
    def __init__(self) -> None:
        self.references: list[EventReference] = []
        self.called = asyncio.Event()

    async def publish(self, reference: EventReference) -> None:
        self.references.append(reference)
        self.called.set()


class FakeStdin:
    def __init__(self, process: FakeProcess) -> None:
        self.process = process
        self.closed = False

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.process.finish(0)


class FakeProcess:
    def __init__(self, *, ready: bool = True) -> None:
        self.returncode: int | None = None
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stdin = FakeStdin(self)
        self.terminated = False
        self._done = asyncio.Event()
        if ready:
            self.stderr.feed_data(f"[event] ready event_key={BOT_ADDED_EVENT_KEY}\n".encode())

    def feed_event(self, payload: object) -> None:
        self.stdout.feed_data((json.dumps(payload) + "\n").encode())

    def finish(self, returncode: int) -> None:
        if self.returncode is not None:
            return
        self.returncode = returncode
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._done.set()

    def terminate(self) -> None:
        self.terminated = True
        self.finish(0)

    async def wait(self) -> int:
        await self._done.wait()
        assert self.returncode is not None
        return self.returncode


def bot_added_event(*, event_id: str = "evt_1", chat_id: str = "oc_group") -> dict[str, Any]:
    return {
        "schema": "2.0",
        "header": {
            "event_id": event_id,
            "event_type": BOT_ADDED_EVENT_KEY,
        },
        "event": {"chat_id": chat_id, "name": "项目群"},
    }


def install_processes(
    monkeypatch: pytest.MonkeyPatch, *processes: FakeProcess
) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    queue = list(processes)
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def create(*args: str, **kwargs: Any) -> FakeProcess:
        calls.append((args, kwargs))
        return queue.pop(0)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    return calls


@pytest.mark.asyncio
async def test_source_publishes_bot_added_reference_and_stops_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess()
    calls = install_processes(monkeypatch, process)
    publisher = FakePublisher()
    source = LarkLongConnectionEventSource(
        LarkCli("lark-cli"), publisher.publish, shutdown_timeout_seconds=0.5
    )

    await source.start()
    process.feed_event(bot_added_event())
    await asyncio.wait_for(publisher.called.wait(), timeout=1)
    await source.stop()

    assert publisher.references == [
        EventReference(event_id="evt_1", event_type=BOT_ADDED_EVENT_KEY, chat_id="oc_group")
    ]
    assert calls[0][0] == (
        "lark-cli",
        "event",
        "consume",
        BOT_ADDED_EVENT_KEY,
        "--as",
        "bot",
    )
    assert process.stdin.closed is True
    assert process.terminated is False


@pytest.mark.asyncio
async def test_duplicate_events_are_published_for_idempotent_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess()
    install_processes(monkeypatch, process)
    publisher = FakePublisher()
    source = LarkLongConnectionEventSource(
        LarkCli("lark-cli"), publisher.publish, shutdown_timeout_seconds=0.5
    )

    await source.start()
    process.feed_event(bot_added_event())
    process.feed_event(bot_added_event())
    async with asyncio.timeout(1):
        while len(publisher.references) < 2:
            await asyncio.sleep(0)
    await source.stop()

    assert [reference.chat_id for reference in publisher.references] == ["oc_group", "oc_group"]


@pytest.mark.asyncio
async def test_malformed_event_is_skipped_without_stopping_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess()
    install_processes(monkeypatch, process)
    publisher = FakePublisher()
    source = LarkLongConnectionEventSource(
        LarkCli("lark-cli"), publisher.publish, shutdown_timeout_seconds=0.5
    )

    await source.start()
    process.feed_event({"event": {"chat_id": "not-a-chat"}})
    process.feed_event(bot_added_event(chat_id="oc_valid"))
    await asyncio.wait_for(publisher.called.wait(), timeout=1)
    await source.stop()

    assert publisher.references[0].chat_id == "oc_valid"


@pytest.mark.asyncio
async def test_start_fails_when_consumer_exits_before_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(ready=False)
    process.stderr.feed_data(
        json.dumps(
            {
                "ok": False,
                "error": {
                    "type": "authorization",
                    "subtype": "missing_scope",
                    "code": 99991672,
                },
            }
        ).encode()
        + b"\n"
    )
    process.finish(3)
    install_processes(monkeypatch, process)
    source = LarkLongConnectionEventSource(
        LarkCli("lark-cli"), FakePublisher().publish, startup_timeout_seconds=0.5
    )

    with pytest.raises(Codex2LarkError, match="before becoming ready"):
        await source.start()


@pytest.mark.asyncio
async def test_runtime_exit_restarts_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    first = FakeProcess()
    second = FakeProcess()
    calls = install_processes(monkeypatch, first, second)
    monkeypatch.setattr(event_module, "_RESTART_DELAYS", (0.01,))
    source = LarkLongConnectionEventSource(
        LarkCli("lark-cli"), FakePublisher().publish, shutdown_timeout_seconds=0.5
    )

    await source.start()
    first.finish(4)
    async with asyncio.timeout(1):
        while len(calls) < 2:
            await asyncio.sleep(0)
    await source.stop()

    assert len(calls) == 2
    assert second.stdin.closed is True
