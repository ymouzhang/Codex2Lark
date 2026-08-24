from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from codex2lark.adapters.lark_cli import LarkCli
from codex2lark.core.errors import ErrorCategory, LarkCliError


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
async def test_auth_status_bare_object_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    process = FakeProcess(
        0,
        json.dumps(
            {
                "appId": "cli_test",
                "identities": {
                    "user": {"status": "ready", "available": True},
                    "bot": {"status": "missing", "available": False},
                },
                "identity": "user",
            }
        ),
    )
    calls = install_process(monkeypatch, process)
    client = LarkCli("lark-cli")

    result = await client.auth_status()

    assert result.identity == "user"
    assert result.data["identities"]["user"]["available"] is True
    assert calls[0][0] == ("lark-cli", "auth", "status", "--json", "--verify")


@pytest.mark.asyncio
async def test_version_output_is_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = install_process(monkeypatch, FakeProcess(0, "lark-cli version 1.0.89\n"))
    client = LarkCli("lark-cli")

    version = await client.version()

    assert version == "1.0.89"
    assert calls[0][0] == ("lark-cli", "--version")


@pytest.mark.asyncio
async def test_invalid_version_output_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    install_process(monkeypatch, FakeProcess(0, "1.0.89"))
    client = LarkCli("lark-cli")

    with pytest.raises(LarkCliError, match="invalid version response"):
        await client.version()


@pytest.mark.asyncio
async def test_bare_status_is_still_rejected_by_normal_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_process(
        monkeypatch,
        FakeProcess(0, json.dumps({"identities": {}, "identity": "none"})),
    )
    client = LarkCli("lark-cli")

    with pytest.raises(LarkCliError) as raised:
        await client.execute(["docs", "+fetch"])

    assert raised.value.category is ErrorCategory.UPSTREAM


@pytest.mark.asyncio
async def test_malformed_auth_status_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    install_process(monkeypatch, FakeProcess(0, json.dumps({"identity": "user"})))
    client = LarkCli("lark-cli")

    with pytest.raises(LarkCliError, match="invalid authentication status"):
        await client.auth_status()


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
