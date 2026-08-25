from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from codex2lark.bootstrap.process_control import (
    GatewayProcessController,
    GatewayStatusFiles,
)


def test_status_files_are_content_safe_and_owner_scoped(tmp_path: Path) -> None:
    files = GatewayStatusFiles(tmp_path.resolve())
    files.publish(
        "ready",
        pid=os.getpid(),
        started_at_ms=123,
        source_state="connected",
        provider_state="ready",
    )

    status = files.read()

    assert status.ok and status.state == "ready"
    assert set(GatewayStatusFiles.as_json(status))
    assert os.stat(files.pid_path).st_mode & 0o777 == 0o600
    files.clear_if_owner(os.getpid() + 1)
    assert files.pid_path.exists()
    files.clear_if_owner(os.getpid())
    assert not files.pid_path.exists()


def test_status_files_publish_content_safe_source_degradation(tmp_path: Path) -> None:
    files = GatewayStatusFiles(tmp_path.resolve())
    files.publish(
        "degraded",
        pid=os.getpid(),
        started_at_ms=123,
        source_state="reconnecting",
        provider_state="ready",
        reconnect_attempts=3,
    )

    status = files.read()
    encoded = GatewayStatusFiles.as_json(status)

    assert not status.ok and status.state == "degraded"
    assert status.source_state == "reconnecting"
    assert status.reconnect_attempts == 3
    assert "error" not in encoded and "credential" not in encoded
    files.clear_if_owner(os.getpid())


def test_daemon_start_uses_argument_array_and_waits_for_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    controller = GatewayProcessController(tmp_path.resolve())
    captured: dict[str, Any] = {}

    class FakeProcess:
        pid = 424242
        returncode: int | None = None

        def poll(self) -> int | None:
            return None

    def fake_popen(arguments: list[str], **options: object) -> FakeProcess:
        captured["arguments"] = arguments
        captured["options"] = options
        threading.Timer(
            0.02,
            lambda: controller.files.publish(
                "ready",
                pid=424242,
                started_at_ms=100,
                source_state="connected",
                provider_state="ready",
            ),
        ).start()
        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(GatewayStatusFiles, "_alive", staticmethod(lambda _pid: True))

    status = controller.start(timeout_seconds=1)

    assert status.state == "ready"
    assert captured["arguments"] == [
        sys.executable,
        "-m",
        "codex2lark",
        "gateway",
        "run",
    ]
    assert captured["options"]["start_new_session"] is True
    assert captured["options"]["close_fds"] is True


def test_stop_refuses_mismatched_pid_without_signaling(tmp_path: Path) -> None:
    controller = GatewayProcessController(tmp_path.resolve())
    controller.files.publish(
        "ready",
        pid=os.getpid(),
        started_at_ms=1,
        source_state="connected",
        provider_state="ready",
    )

    with pytest.raises(RuntimeError, match="not a Codex2Lark Gateway"):
        controller.stop(timeout_seconds=0.1)


def test_stop_signals_validated_gateway_process_and_waits(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
            "-m",
            "codex2lark",
            "gateway",
            "run",
        ],
        start_new_session=True,
    )
    controller = GatewayProcessController(tmp_path.resolve())
    controller.files.publish(
        "ready",
        pid=process.pid,
        started_at_ms=int(time.time() * 1000),
        source_state="connected",
        provider_state="ready",
    )
    try:
        status = controller.stop(timeout_seconds=2)
        process.wait(timeout=2)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=2)

    assert status.state == "stopped"
    assert not controller.files.pid_path.exists()
