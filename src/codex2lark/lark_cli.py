from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import Codex2LarkError, ErrorCategory, LarkCliError

_SECRET_PATTERN = re.compile(
    r"(?i)(app_secret|access_token|refresh_token|authorization)"
    r"([\"'\s:=]+)(?:Bearer\s+)?([^\s\"']+)"
)


def redact_secrets(value: str) -> str:
    return _SECRET_PATTERN.sub(r"\1\2[REDACTED]", value)


@dataclass(frozen=True, slots=True)
class LarkCliResult:
    data: dict[str, Any]
    identity: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    warnings: tuple[str, ...] = ()


class LarkCli:
    def __init__(
        self,
        executable: str | Sequence[str] = "lark-cli",
        *,
        timeout_seconds: float = 120.0,
        max_output_bytes: int = 8_000_000,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.executable = (executable,) if isinstance(executable, str) else tuple(executable)
        if not self.executable:
            raise ValueError("executable cannot be empty")
        self.timeout_seconds = timeout_seconds
        self.max_output_bytes = max_output_bytes
        self.environment = dict(environment or {})

    def available(self) -> bool:
        command = self.executable[0]
        return Path(command).is_file() or shutil.which(command) is not None

    async def execute(
        self,
        args: Sequence[str],
        *,
        cwd: Path | None = None,
    ) -> LarkCliResult:
        argv = (*self.executable, *self._validated_args(args))
        env = os.environ.copy()
        env.update(
            {
                "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
            }
        )
        env.update(self.environment)

        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise LarkCliError(
                ErrorCategory.VALIDATION,
                "lark-cli is not installed or is not available on PATH",
            ) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=self.timeout_seconds
            )
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise LarkCliError(
                ErrorCategory.TIMEOUT,
                "lark-cli operation timed out",
                details={"timeout_seconds": self.timeout_seconds},
            ) from exc

        if len(stdout) > self.max_output_bytes or len(stderr) > self.max_output_bytes:
            raise LarkCliError(
                ErrorCategory.UPSTREAM,
                "lark-cli output exceeded the configured limit",
            )

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()
        payload_text = stdout_text if process.returncode == 0 else (stderr_text or stdout_text)
        payload = self._parse_payload(payload_text)

        if process.returncode != 0 or payload.get("ok") is not True:
            raise self._upstream_error(payload, process.returncode, stderr_text)

        data = payload.get("data")
        if not isinstance(data, dict):
            data = {"value": data}
        raw_meta = payload.get("meta")
        meta: dict[str, Any] = dict(raw_meta) if isinstance(raw_meta, dict) else {}
        warnings_value = data.get("warnings", payload.get("warnings", []))
        warnings = tuple(str(item) for item in warnings_value) if warnings_value else ()
        return LarkCliResult(
            data=data,
            identity=payload.get("identity"),
            meta=meta,
            warnings=warnings,
        )

    @staticmethod
    def _validated_args(args: Sequence[str]) -> tuple[str, ...]:
        result: list[str] = []
        for arg in args:
            if not isinstance(arg, str):
                raise TypeError("all lark-cli arguments must be strings")
            if "\x00" in arg:
                raise ValueError("lark-cli arguments must not contain NUL bytes")
            result.append(arg)
        return tuple(result)

    @staticmethod
    def _parse_payload(value: str) -> dict[str, Any]:
        if not value:
            raise LarkCliError(
                ErrorCategory.UPSTREAM,
                "lark-cli returned an empty response",
            )
        try:
            payload = json.loads(value)
        except json.JSONDecodeError as exc:
            raise LarkCliError(
                ErrorCategory.UPSTREAM,
                "lark-cli returned a non-JSON response",
                details={"response": redact_secrets(value[:500])},
            ) from exc
        if not isinstance(payload, dict):
            raise LarkCliError(ErrorCategory.UPSTREAM, "lark-cli returned an invalid envelope")
        return payload

    @staticmethod
    def _upstream_error(
        payload: dict[str, Any], return_code: int | None, stderr: str
    ) -> LarkCliError:
        raw_error = payload.get("error")
        error: dict[str, Any] = dict(raw_error) if isinstance(raw_error, dict) else {}
        error_type = str(error.get("type", ""))
        message = str(error.get("message") or "lark-cli operation failed")
        category = ErrorCategory.UPSTREAM
        if "auth" in error_type or "authorization" in message.lower():
            category = ErrorCategory.AUTHENTICATION
        elif "permission" in error_type or "scope" in message.lower():
            category = ErrorCategory.PERMISSION
        details: dict[str, Any] = {"return_code": return_code}
        for key in ("code", "subtype", "log_id", "hint"):
            if key in error:
                details[key] = redact_secrets(str(error[key]))
        if not error and stderr:
            details["stderr"] = redact_secrets(stderr[:500])
        return LarkCliError(category, redact_secrets(message), details=details)


def safe_tool_call_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, Codex2LarkError):
        return exc.as_dict()
    return Codex2LarkError(
        ErrorCategory.INTERNAL,
        "unexpected internal error",
    ).as_dict()
