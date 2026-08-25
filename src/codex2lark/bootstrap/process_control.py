from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from contextlib import suppress
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class GatewayProcessStatus:
    ok: bool
    state: str
    pid: int | None
    started_at_ms: int | None
    source_state: str | None = None
    reconnect_attempts: int = 0


class GatewayStatusFiles:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir.resolve()
        self.pid_path = self.data_dir / "gateway.pid"
        self.status_path = self.data_dir / "gateway-status.json"

    def publish(
        self,
        state: str,
        *,
        pid: int,
        started_at_ms: int,
        source_state: str | None = None,
        reconnect_attempts: int = 0,
    ) -> None:
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._atomic_write(self.pid_path, f"{pid}\n".encode())
        self._atomic_write(
            self.status_path,
            json.dumps(
                {
                    "state": state,
                    "pid": pid,
                    "started_at_ms": started_at_ms,
                    "source_state": source_state,
                    "reconnect_attempts": reconnect_attempts,
                },
                separators=(",", ":"),
                sort_keys=True,
            ).encode(),
        )

    def clear_if_owner(self, pid: int) -> None:
        if self.read_pid() != pid:
            return
        self.status_path.unlink(missing_ok=True)
        self.pid_path.unlink(missing_ok=True)

    def read_pid(self) -> int | None:
        try:
            value = int(self.pid_path.read_text().strip())
        except (FileNotFoundError, OSError, ValueError):
            return None
        return value if value > 0 else None

    def read(self) -> GatewayProcessStatus:
        pid = self.read_pid()
        if pid is None:
            return GatewayProcessStatus(True, "stopped", None, None)
        if not self._alive(pid):
            return GatewayProcessStatus(False, "stale", pid, None)
        try:
            payload = json.loads(self.status_path.read_text())
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return GatewayProcessStatus(False, "starting", pid, None)
        state = payload.get("state")
        started_at_ms = payload.get("started_at_ms")
        if state not in {"starting", "ready", "degraded", "stopping"}:
            return GatewayProcessStatus(False, "invalid", pid, None)
        source_state = payload.get("source_state")
        reconnect_attempts = payload.get("reconnect_attempts", 0)
        normalized_source = source_state if isinstance(source_state, str) else None
        if state == "ready" and normalized_source != "connected":
            return GatewayProcessStatus(False, "invalid", pid, None)
        if state == "degraded" and normalized_source not in {
            "starting",
            "reconnecting",
            "error",
        }:
            return GatewayProcessStatus(False, "invalid", pid, None)
        return GatewayProcessStatus(
            state == "ready",
            state,
            pid,
            started_at_ms if isinstance(started_at_ms, int) else None,
            normalized_source,
            (
                reconnect_attempts
                if isinstance(reconnect_attempts, int)
                and not isinstance(reconnect_attempts, bool)
                and reconnect_attempts >= 0
                else 0
            ),
        )

    @staticmethod
    def as_json(status: GatewayProcessStatus) -> str:
        return json.dumps(asdict(status), sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_name, path)
        finally:
            Path(temporary_name).unlink(missing_ok=True)

    @staticmethod
    def _alive(pid: int) -> bool:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        except (OSError, IndexError):
            state = None
        if state == "Z":
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


class GatewayProcessController:
    def __init__(self, data_dir: Path) -> None:
        if not data_dir.is_absolute():
            raise ValueError("data directory must be absolute")
        self.data_dir = data_dir.resolve()
        self.files = GatewayStatusFiles(self.data_dir)
        self.log_path = self.data_dir / "gateway.log"

    def start(self, *, timeout_seconds: float = 35.0) -> GatewayProcessStatus:
        current = self.files.read()
        if current.pid is not None and current.state != "stale":
            raise RuntimeError(f"Gateway is already {current.state}")
        if current.state == "stale":
            self.files.clear_if_owner(current.pid or -1)
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment["CODEX2LARK_DAEMON_CHILD"] = "1"
        started_at_ms = int(time.time() * 1000)
        with self.log_path.open("ab") as log:
            os.chmod(self.log_path, 0o600)
            process = subprocess.Popen(
                [sys.executable, "-m", "codex2lark", "gateway", "run"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                env=environment,
                start_new_session=True,
                close_fds=True,
            )
        self.files.publish("starting", pid=process.pid, started_at_ms=started_at_ms)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                self.files.clear_if_owner(process.pid)
                raise RuntimeError(f"Gateway exited during startup with code {process.returncode}")
            status = self.files.read()
            if status.ok and status.pid == process.pid:
                return status
            time.sleep(0.05)
        process.terminate()
        with suppress(subprocess.TimeoutExpired):
            process.wait(timeout=5)
        self.files.clear_if_owner(process.pid)
        raise TimeoutError("Gateway did not become ready before the startup deadline")

    def stop(self, *, timeout_seconds: float = 30.0) -> GatewayProcessStatus:
        status = self.files.read()
        if status.pid is None:
            return status
        if status.state == "stale":
            raise RuntimeError("Gateway PID state is stale; no process was signaled")
        self._validate_process(status.pid)
        self.files.publish(
            "stopping",
            pid=status.pid,
            started_at_ms=status.started_at_ms or int(time.time() * 1000),
        )
        os.kill(status.pid, signal.SIGTERM)
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if not GatewayStatusFiles._alive(status.pid):
                self.files.clear_if_owner(status.pid)
                return GatewayProcessStatus(True, "stopped", None, None)
            time.sleep(0.05)
        raise TimeoutError("Gateway did not stop before the graceful shutdown deadline")

    @staticmethod
    def _validate_process(pid: int) -> None:
        command_path = Path(f"/proc/{pid}/cmdline")
        try:
            command = [
                item.decode(errors="replace")
                for item in command_path.read_bytes().split(b"\x00")
                if item
            ]
        except OSError as exc:
            raise RuntimeError("cannot validate the recorded Gateway process") from exc
        expected = ["-m", "codex2lark", "gateway", "run"]
        if not any(
            command[index : index + len(expected)] == expected
            for index in range(len(command) - len(expected) + 1)
        ):
            raise RuntimeError("recorded PID is not a Codex2Lark Gateway; no signal was sent")
