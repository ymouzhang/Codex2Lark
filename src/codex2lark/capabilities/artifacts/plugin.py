from __future__ import annotations

from codex2lark.core.models import Identity
from codex2lark.runtime.plugins import PluginHealth, PluginManifest
from codex2lark.runtime.tools import SemanticTool

from .tools import ArtifactService, artifact_tools


class FeishuArtifactsPlugin:
    manifest = PluginManifest(
        plugin_id="feishu-artifacts",
        version="1.0.0",
        runtime_api=1,
        capabilities=(
            "whiteboard.render",
            "sheets.workbook.create",
            "sheets.range.write",
            "base.app.create",
            "base.records.upsert",
        ),
        required_scopes=(
            "board:whiteboard:node:create",
            "sheets:spreadsheet:create",
            "sheets:spreadsheet:write_only",
            "base:app:create",
            "base:record:create",
            "base:record:update",
        ),
        resources=("resources/authoring",),
    )

    def __init__(self, service: ArtifactService, identity: Identity) -> None:
        self.tools: tuple[SemanticTool, ...] = tuple(artifact_tools(service, identity))
        self._started = False

    async def initialize(self) -> None:
        self._started = True

    async def health(self) -> PluginHealth:
        return PluginHealth(self._started, None if self._started else "plugin is not initialized")

    async def stop(self) -> None:
        self._started = False
