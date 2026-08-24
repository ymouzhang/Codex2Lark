from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Awaitable, Callable
from contextlib import suppress

from ..adapters.lark_cli import LarkCli, redact_secrets, safe_tool_call_error
from ..core.errors import Codex2LarkError, ErrorCategory
from .delivery import EventReference
from .handlers import BOT_ADDED_EVENT_KEY

_READY_MARKER = f"[event] ready event_key={BOT_ADDED_EVENT_KEY}"
_MAX_EVENT_BYTES = 256_000
_RESTART_DELAYS = (1.0, 2.0, 5.0, 10.0, 30.0)

logger = logging.getLogger(__name__)


class LarkLongConnectionEventSource:
    """Owns one fixed lark-cli long-connection subscription."""

    def __init__(
        self,
        lark: LarkCli,
        publish: Callable[[EventReference], Awaitable[None]],
        *,
        startup_timeout_seconds: float = 20.0,
        shutdown_timeout_seconds: float = 10.0,
    ) -> None:
        self.lark = lark
        self.publish = publish
        self.startup_timeout_seconds = startup_timeout_seconds
        self.shutdown_timeout_seconds = shutdown_timeout_seconds
        self._stop = asyncio.Event()
        self._process: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("bot-added event supervisor is already running")
        self._stop.clear()
        process = await self._spawn_ready()
        self._process = process
        self._task = asyncio.create_task(
            self._supervise(process), name="codex2lark-bot-added-events"
        )

    async def stop(self) -> None:
        self._stop.set()
        process = self._process
        if process is not None and process.stdin is not None:
            process.stdin.close()
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self.shutdown_timeout_seconds)
        except TimeoutError:
            process = self._process
            if process is not None and process.returncode is None:
                process.terminate()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self.shutdown_timeout_seconds)
            except TimeoutError:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        finally:
            self._task = None
            self._process = None

    async def _supervise(self, initial: asyncio.subprocess.Process) -> None:
        process = initial
        restart_index = 0
        while True:
            try:
                return_code = await self._consume(process)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = safe_tool_call_error(exc)["error"]
                logger.error(
                    "bot-added event consumer failed; category=%s message=%s",
                    error["category"],
                    error["message"],
                )
                return_code = None
            finally:
                await self._close_process(process)
                if self._process is process:
                    self._process = None
            if self._stop.is_set():
                return
            logger.warning(
                "bot-added event consumer exited unexpectedly; return_code=%s",
                return_code,
            )
            delay = _RESTART_DELAYS[min(restart_index, len(_RESTART_DELAYS) - 1)]
            restart_index += 1
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
            try:
                process = await self._spawn_ready()
            except Exception as exc:
                error = safe_tool_call_error(exc)["error"]
                logger.error(
                    "bot-added event consumer restart failed; category=%s message=%s",
                    error["category"],
                    error["message"],
                )
                continue
            self._process = process
            restart_index = 0

    async def _spawn_ready(self) -> asyncio.subprocess.Process:
        env = os.environ.copy()
        env.update(
            {
                "LARKSUITE_CLI_NO_UPDATE_NOTIFIER": "1",
                "LARKSUITE_CLI_NO_SKILLS_NOTIFIER": "1",
            }
        )
        env.update(self.lark.environment)
        argv = (
            *self.lark.executable,
            "event",
            "consume",
            BOT_ADDED_EVENT_KEY,
            "--as",
            "bot",
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise Codex2LarkError(
                ErrorCategory.VALIDATION,
                "lark-cli is not installed or is not available on PATH",
            ) from exc
        try:
            await asyncio.wait_for(self._wait_ready(process), timeout=self.startup_timeout_seconds)
        except TimeoutError as exc:
            await self._close_process(process)
            raise Codex2LarkError(
                ErrorCategory.TIMEOUT,
                "bot-added event consumer did not become ready before timeout",
                details={"timeout_seconds": self.startup_timeout_seconds},
            ) from exc
        except Exception:
            await self._close_process(process)
            raise
        return process

    async def _wait_ready(self, process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            raise Codex2LarkError(
                ErrorCategory.UPSTREAM,
                "bot-added event consumer did not expose stderr",
            )
        while True:
            line = await process.stderr.readline()
            if not line:
                return_code = await process.wait()
                raise Codex2LarkError(
                    ErrorCategory.UPSTREAM,
                    "bot-added event consumer exited before becoming ready",
                    details={"return_code": return_code},
                )
            text = line.decode("utf-8", errors="replace").strip()
            if text == _READY_MARKER:
                return
            self._log_diagnostic(text, startup=True)

    async def _consume(self, process: asyncio.subprocess.Process) -> int:
        if process.stdout is None or process.stderr is None:
            raise Codex2LarkError(
                ErrorCategory.UPSTREAM,
                "bot-added event consumer did not expose output streams",
            )
        stderr_task = asyncio.create_task(self._drain_stderr(process.stderr))
        try:
            while line := await process.stdout.readline():
                await self._handle_line(line)
            return await process.wait()
        finally:
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task

    async def _handle_line(self, line: bytes) -> None:
        if len(line) > _MAX_EVENT_BYTES:
            logger.error("bot-added event exceeded the in-memory size limit and was skipped")
            return
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.error("bot-added event was not valid JSON and was skipped")
            return
        if not isinstance(payload, dict):
            logger.error("bot-added event was not a JSON object and was skipped")
            return
        event = payload.get("event")
        header = payload.get("header")
        chat_id = event.get("chat_id") if isinstance(event, dict) else None
        event_id = header.get("event_id") if isinstance(header, dict) else None
        if not isinstance(chat_id, str) or not chat_id.startswith("oc_"):
            logger.error("bot-added event did not contain a valid chat_id and was skipped")
            return
        safe_event_id = event_id if isinstance(event_id, str) else "unknown"
        event_type = header.get("event_type") if isinstance(header, dict) else None
        if event_type != BOT_ADDED_EVENT_KEY:
            logger.error("event type did not match the fixed subscription and was skipped")
            return
        await self.publish(
            EventReference(
                event_id=safe_event_id,
                event_type=event_type,
                chat_id=chat_id,
            )
        )

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while line := await stream.readline():
            self._log_diagnostic(line.decode("utf-8", errors="replace").strip(), startup=False)

    @staticmethod
    def _log_diagnostic(value: str, *, startup: bool) -> None:
        if not value or value.startswith("[event]"):
            return
        if value.startswith("[source]"):
            logger.info("%s", redact_secrets(value)[:500])
            return
        try:
            payload = json.loads(value)
        except json.JSONDecodeError:
            logger.warning(
                "bot-added event consumer emitted an unstructured %s diagnostic",
                "startup" if startup else "runtime",
            )
            return
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict):
            logger.warning("bot-added event consumer emitted a diagnostic without an error")
            return
        logger.error(
            "bot-added event consumer error; type=%s subtype=%s code=%s hint=%s",
            error.get("type", "unknown"),
            error.get("subtype", "unknown"),
            error.get("code", "unknown"),
            redact_secrets(str(error.get("hint", "none")))[:500],
        )

    @staticmethod
    async def _close_process(process: asyncio.subprocess.Process) -> None:
        if process.stdin is not None:
            process.stdin.close()
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()
        with suppress(ProcessLookupError):
            await process.wait()
