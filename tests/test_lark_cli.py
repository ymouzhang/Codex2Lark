from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from codex2lark.errors import ErrorCategory, LarkCliError
from codex2lark.lark_cli import LarkCli


class FakeProcess:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout.encode()
        self.stderr = stderr.encode()
        self.killed = False

    async def communicate(self) -> tuple[bytes, bytes]:
        return self.stdout, self.stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


def install_process(
    monkeypatch: pytest.MonkeyPatch,
    process: FakeProcess,
) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
    calls: list[tuple[tuple[str, ...], dict[str, Any]]] = []

    async def create(*args: str, **kwargs: Any) -> FakeProcess:
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    return calls


@pytest.mark.asyncio
async def test_success_envelope_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(
        0,
        json.dumps(
            {
                "ok": True,
                "identity": "user",
                "data": {"document": {"revision_id": 7}},
            }
        ),
    )
    calls = install_process(monkeypatch, process)
    client = LarkCli("lark-cli")

    result = await client.execute(["docs", "+fetch"])

    assert result.identity == "user"
    assert result.data["document"]["revision_id"] == 7
    assert calls[0][0] == ("lark-cli", "docs", "+fetch")
    assert calls[0][1]["env"]["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] == "1"


@pytest.mark.asyncio
async def test_permission_error_is_classified_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = FakeProcess(
        1,
        stderr=json.dumps(
            {
                "ok": False,
                "error": {
                    "type": "api",
                    "message": "missing required scope",
                    "hint": "authorization: Bearer secret-token",
                },
            }
        ),
    )
    install_process(monkeypatch, process)
    client = LarkCli("lark-cli")

    with pytest.raises(LarkCliError) as raised:
        await client.execute(["docs", "+fetch"])

    assert raised.value.category is ErrorCategory.PERMISSION
    assert "secret-token" not in str(raised.value.details)


@pytest.mark.asyncio
async def test_non_json_output_is_rejected_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_process(
        monkeypatch,
        FakeProcess(0, stdout="access_token=secret-token unexpected output"),
    )
    client = LarkCli("lark-cli")

    with pytest.raises(LarkCliError) as raised:
        await client.execute(["docs", "+fetch"])

    assert raised.value.category is ErrorCategory.UPSTREAM
    assert "secret-token" not in str(raised.value.details)


@pytest.mark.asyncio
async def test_nul_argument_is_rejected_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = install_process(monkeypatch, FakeProcess(0))
    client = LarkCli("lark-cli")

    with pytest.raises(ValueError):
        await client.execute(["bad\x00argument"])

    assert calls == []
