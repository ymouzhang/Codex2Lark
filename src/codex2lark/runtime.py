from __future__ import annotations

import tempfile
from pathlib import Path
from types import TracebackType

from .errors import Codex2LarkError, ErrorCategory


class EphemeralWorkspace:
    """A per-request workspace that is always removed when its context exits."""

    def __init__(self, *, max_file_bytes: int = 8_000_000) -> None:
        self.max_file_bytes = max_file_bytes
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self.path: Path | None = None

    def __enter__(self) -> EphemeralWorkspace:
        self._temporary_directory = tempfile.TemporaryDirectory(prefix="codex2lark-")
        self.path = Path(self._temporary_directory.name).resolve()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
        self.path = None
        self._temporary_directory = None

    def write_text(self, filename: str, content: str) -> Path:
        if self.path is None:
            raise RuntimeError("workspace is not active")
        size = len(content.encode("utf-8"))
        if size > self.max_file_bytes:
            raise Codex2LarkError(
                ErrorCategory.VALIDATION,
                "ephemeral file exceeds the configured size limit",
                details={"bytes": size, "max_bytes": self.max_file_bytes},
            )
        target = (self.path / filename).resolve()
        if target.parent != self.path:
            raise Codex2LarkError(
                ErrorCategory.VALIDATION,
                "ephemeral filename must not contain path traversal",
            )
        target.write_text(content, encoding="utf-8")
        return target

    def relative_reference(self, path: Path) -> str:
        if self.path is None or path.parent != self.path:
            raise ValueError("path is not inside the active workspace")
        return f"@./{path.name}"
