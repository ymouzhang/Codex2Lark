from __future__ import annotations

import fcntl
import hashlib
import os
from pathlib import Path
from types import TracebackType


class DataDirectoryLock:
    """One-process ownership for a runtime data directory."""

    def __init__(self, data_dir: Path) -> None:
        resolved = data_dir.resolve()
        digest = hashlib.sha256(str(resolved).encode()).hexdigest()[:16]
        self.path = resolved.parent / f".{resolved.name}.{digest}.lock"
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self._descriptor is not None:
            return
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(descriptor)
            raise RuntimeError("runtime data directory is in use; stop the Gateway first") from exc
        self._descriptor = descriptor

    def release(self) -> None:
        if self._descriptor is None:
            return
        fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        os.close(self._descriptor)
        self._descriptor = None

    def __enter__(self) -> DataDirectoryLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        self.release()
