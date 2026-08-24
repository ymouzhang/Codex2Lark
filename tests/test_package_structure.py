from __future__ import annotations

import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "codex2lark"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}


def test_root_package_contains_only_public_entrypoints() -> None:
    root_modules = {path.name for path in PACKAGE_ROOT.glob("*.py")}

    assert root_modules == {"__init__.py", "__main__.py", "cli.py"}


def test_mcp_interface_does_not_import_gateway_bootstrap() -> None:
    modules = imported_modules(PACKAGE_ROOT / "interfaces" / "mcp.py")

    assert "bootstrap.gateway" not in modules


def test_services_do_not_depend_on_interfaces_or_gateway_bootstrap() -> None:
    for path in (PACKAGE_ROOT / "services").glob("*.py"):
        modules = imported_modules(path)
        assert not any(module.startswith(("interfaces", "bootstrap")) for module in modules), (
            path.name
        )
