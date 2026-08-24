from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from codex2lark import cli
from codex2lark.adapters.lark_cli import LarkCliResult


class FakeLark:
    def __init__(self, status: dict[str, Any], version: str = "1.0.89") -> None:
        self.status = status
        self.installed_version = version

    def available(self) -> bool:
        return True

    async def version(self) -> str:
        return self.installed_version

    async def auth_status(self, *, verify: bool = True) -> LarkCliResult:
        assert verify is True
        return LarkCliResult(data=self.status, identity=self.status.get("identity"))


def test_parser_exposes_independent_gateway_command() -> None:
    assert cli._parser().parse_args(["gateway"]).command == "gateway"


@pytest.mark.asyncio
async def test_doctor_accepts_available_active_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = {
        "identity": "user",
        "identities": {"user": {"status": "ready", "available": True}},
    }
    monkeypatch.setattr(cli, "create_application", lambda: SimpleNamespace(lark=FakeLark(status)))

    result = await cli._doctor()
    output = json.loads(capsys.readouterr().out)

    assert result == 0
    assert output["ok"] is True
    assert output["checks"]["authentication"] == status


@pytest.mark.asyncio
async def test_doctor_rejects_unavailable_authentication(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = {
        "identity": "none",
        "identities": {"user": {"status": "verify_failed", "available": False}},
    }
    monkeypatch.setattr(cli, "create_application", lambda: SimpleNamespace(lark=FakeLark(status)))

    result = await cli._doctor()
    output = json.loads(capsys.readouterr().out)

    assert result == 1
    assert output["ok"] is False
    assert output["checks"]["lark_cli_version"] == "1.0.89"
    assert output["checks"]["authentication"] == status
    assert "auth login" in output["next_action"]


@pytest.mark.asyncio
async def test_doctor_rejects_unpinned_lark_cli_version(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    status = {
        "identity": "user",
        "identities": {"user": {"status": "ready", "available": True}},
    }
    lark = FakeLark(status, version="1.0.90")
    monkeypatch.setattr(cli, "create_application", lambda: SimpleNamespace(lark=lark))

    result = await cli._doctor()
    output = json.loads(capsys.readouterr().out)

    assert result == 1
    assert output["ok"] is False
    assert output["checks"]["lark_cli_version"] == {
        "installed": "1.0.90",
        "required": "1.0.89",
    }
    assert "@larksuite/cli@1.0.89" in output["next_action"]
