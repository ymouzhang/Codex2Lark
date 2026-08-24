from __future__ import annotations

import asyncio
import base64
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


def test_parser_exposes_storage_administration_commands(tmp_path) -> None:
    arguments = cli._parser().parse_args(
        ["storage", "restore", str(tmp_path / "backup.zip"), "--data-dir", str(tmp_path)]
    )
    assert arguments.command == "storage"
    assert arguments.storage_command == "restore"


def test_parser_exposes_live_multigroup_acceptance() -> None:
    arguments = cli._parser().parse_args(
        [
            "acceptance",
            "live-multigroup",
            "--chat-id",
            "oc_first",
            "--chat-id",
            "oc_second",
            "--since-ms",
            "1000",
        ]
    )

    assert arguments.command == "acceptance"
    assert arguments.chat_id == ["oc_first", "oc_second"]


def test_gateway_doctor_validates_configuration_without_printing_secrets(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path,
) -> None:
    values = {
        "CODEX2LARK_FEISHU_APP_ID": "cli_app",
        "CODEX2LARK_FEISHU_APP_SECRET": "feishu-secret-value",
        "OPENAI_API_KEY": "openai-secret-value",
        "CODEX2LARK_MODEL": "configured-model",
        "CODEX2LARK_MODEL_INPUT_COST_MICROS_PER_MILLION_TOKENS": "1250000",
        "CODEX2LARK_MODEL_OUTPUT_COST_MICROS_PER_MILLION_TOKENS": "10000000",
        "CODEX2LARK_MASTER_KEY_ID": "key-v1",
        "CODEX2LARK_MASTER_KEY_BASE64": base64.b64encode(b"k" * 32).decode(),
        "CODEX2LARK_DATA_DIR": str(tmp_path),
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)

    assert cli.main(["doctor", "--gateway"]) == 0
    raw = capsys.readouterr().out
    output = json.loads(raw)

    assert output["checks"]["storage"] == "not_initialized"
    assert output["checks"]["agent_resources"]["group-agent-core"] == "1.1.0"
    assert "feishu-secret-value" not in raw
    assert "openai-secret-value" not in raw


def test_gateway_reports_invalid_configuration_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        cli.GatewayConfig,
        "from_environment",
        lambda: (_ for _ in ()).throw(ValueError("missing gateway secret")),
    )

    assert cli.main(["gateway"]) == 2
    assert "missing gateway secret" in caplog.text


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


@pytest.mark.asyncio
async def test_doctor_has_one_bounded_lark_cli_deadline(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class HangingLark(FakeLark):
        async def version(self) -> str:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

    monkeypatch.setattr(cli, "_DOCTOR_DEADLINE_SECONDS", 0.001)
    monkeypatch.setattr(
        cli,
        "create_application",
        lambda: SimpleNamespace(lark=HangingLark({})),
    )

    result = await cli._doctor()
    output = json.loads(capsys.readouterr().out)

    assert result == 1
    assert output["error"]["category"] == "timeout"
    assert output["error"]["details"]["deadline_seconds"] == 0.001
    assert "auth status" in output["next_action"]
