from __future__ import annotations

from pathlib import Path

import pytest

from codex2lark.errors import Codex2LarkError
from codex2lark.runtime import EphemeralWorkspace


def test_workspace_is_removed_after_success() -> None:
    with EphemeralWorkspace() as workspace:
        path = workspace.write_text("document.xml", "<p>Hello</p>")
        root = path.parent
        assert path.read_text(encoding="utf-8") == "<p>Hello</p>"
    assert not root.exists()


def test_workspace_is_removed_after_failure() -> None:
    root: Path | None = None
    with pytest.raises(RuntimeError), EphemeralWorkspace() as workspace:
        assert workspace.path is not None
        root = workspace.path
        raise RuntimeError("boom")
    assert root is not None
    assert not root.exists()


def test_workspace_rejects_path_traversal() -> None:
    with EphemeralWorkspace() as workspace, pytest.raises(Codex2LarkError):
        workspace.write_text("../outside", "no")
