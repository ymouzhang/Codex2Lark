from __future__ import annotations

import asyncio
import hashlib
import os
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar, cast

from .migrations import INITIAL_SCHEMA, SCHEMA_VERSION

T = TypeVar("T")


@dataclass(slots=True)
class _Request[T]:
    operation: Callable[[sqlite3.Connection], T]
    response: queue.Queue[tuple[bool, object]]


class SQLiteDatabase:
    """Single-connection SQLite actor with an asynchronous caller surface."""

    def __init__(
        self,
        path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        queue_capacity: int = 1_024,
    ) -> None:
        if queue_capacity < 1:
            raise ValueError("queue_capacity must be positive")
        self.path = path.resolve()
        self.busy_timeout_ms = busy_timeout_ms
        self._requests: queue.Queue[_Request[object] | None] = queue.Queue(queue_capacity)
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_failure: BaseException | None = None
        self._started = False
        self._closed = False

    async def open(self) -> None:
        if self._started:
            return
        if self._closed:
            raise RuntimeError("database is closed")
        self._thread = threading.Thread(
            target=self._worker,
            name="codex2lark-sqlite",
            daemon=False,
        )
        self._thread.start()
        self._started = True
        while not self._ready.is_set():
            await asyncio.sleep(0.001)
        if self._startup_failure is not None:
            failure = self._startup_failure
            self._thread.join(timeout=0)
            self._closed = True
            self._started = False
            raise failure

    async def close(self) -> None:
        if self._closed:
            return
        if not self._started or self._thread is None:
            self._closed = True
            return
        await self.call(self._checkpoint)
        while True:
            try:
                self._requests.put_nowait(None)
            except queue.Full:
                await asyncio.sleep(0.001)
            else:
                break
        while self._thread.is_alive():
            await asyncio.sleep(0.001)
        self._thread.join(timeout=0)
        self._closed = True
        self._started = False

    async def call(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        if self._closed:
            raise RuntimeError("database is closed")
        if not self._started:
            raise RuntimeError("database is not open")
        response: queue.Queue[tuple[bool, object]] = queue.Queue(maxsize=1)
        request: _Request[object] = _Request(
            operation=cast(Callable[[sqlite3.Connection], object], operation),
            response=response,
        )
        while True:
            try:
                self._requests.put_nowait(request)
            except queue.Full:
                await asyncio.sleep(0.001)
            else:
                break
        while True:
            try:
                succeeded, value = response.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.001)
            else:
                break
        if succeeded:
            return cast(T, value)
        if isinstance(value, BaseException):
            raise value
        raise RuntimeError("database actor returned an invalid failure")

    async def transaction(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        def invoke(connection: sqlite3.Connection) -> T:
            connection.execute("BEGIN IMMEDIATE")
            try:
                result = operation(connection)
            except BaseException:
                connection.rollback()
                raise
            connection.commit()
            return result

        return await self.call(invoke)

    def _worker(self) -> None:
        connection: sqlite3.Connection | None = None
        try:
            connection = self._create_connection()
            self._ready.set()
            while True:
                request = self._requests.get()
                if request is None:
                    break
                try:
                    result = request.operation(connection)
                except BaseException as exc:
                    request.response.put((False, exc))
                else:
                    request.response.put((True, result))
        except BaseException as exc:
            self._startup_failure = exc
            self._ready.set()
            self._fail_pending(exc)
        finally:
            if connection is not None:
                connection.close()

    def _create_connection(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        connection = sqlite3.connect(self.path, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(f"PRAGMA busy_timeout={self.busy_timeout_ms}")
        connection.execute("PRAGMA synchronous=FULL")
        self._migrate(connection)
        os.chmod(self.path, 0o600)
        return connection

    def _fail_pending(self, failure: BaseException) -> None:
        while True:
            try:
                request = self._requests.get_nowait()
            except queue.Empty:
                return
            if request is not None:
                request.response.put((False, failure))

    @staticmethod
    def _checkpoint(connection: sqlite3.Connection) -> None:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        connection.executescript(INITIAL_SCHEMA)
        checksum = hashlib.sha256(INITIAL_SCHEMA.encode("utf-8")).hexdigest()
        row = connection.execute(
            "SELECT checksum FROM runtime_migrations WHERE version = ?", (SCHEMA_VERSION,)
        ).fetchone()
        if row is not None and row["checksum"] != checksum:
            raise RuntimeError("database migration checksum mismatch")
        if row is None:
            connection.execute(
                "INSERT INTO runtime_migrations(version, checksum, applied_at_ms) VALUES (?, ?, ?)",
                (SCHEMA_VERSION, checksum, int(time.time() * 1000)),
            )
