from __future__ import annotations

from codex2lark.capabilities.artifacts.tools import (
    CreateWorkbookTool,
    SheetsService,
    WriteSheetTool,
)
from codex2lark.core.models import Identity
from codex2lark.runtime.plugins import PluginHealth, PluginManifest
from codex2lark.runtime.tools import SemanticTool


class FeishuSheetsPlugin:
    manifest = PluginManifest(
        plugin_id="feishu-sheets",
        version="1.0.0",
        runtime_api=1,
        capabilities=("sheets.workbook.create", "sheets.range.write"),
        required_scopes=(
            "sheets:spreadsheet:create",
            "sheets:spreadsheet:write_only",
        ),
        resources=("resources/authoring",),
    )

    def __init__(self, service: SheetsService, identity: Identity) -> None:
        self.tools: tuple[SemanticTool, ...] = (
            CreateWorkbookTool(service, identity),
            WriteSheetTool(service, identity),
        )
        self._started = False

    async def initialize(self) -> None:
        self._started = True

    async def health(self) -> PluginHealth:
        return PluginHealth(self._started, None if self._started else "plugin is not initialized")

    async def stop(self) -> None:
        self._started = False
