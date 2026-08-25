from __future__ import annotations

from codex2lark.capabilities.artifacts.tools import RenderWhiteboardTool, WhiteboardService
from codex2lark.core.models import Identity
from codex2lark.runtime.plugins import PluginHealth, PluginManifest
from codex2lark.runtime.tools import SemanticTool


class FeishuWhiteboardPlugin:
    manifest = PluginManifest(
        plugin_id="feishu-whiteboard",
        version="1.0.0",
        runtime_api=1,
        capabilities=("whiteboard.render",),
        required_scopes=("board:whiteboard:node:create",),
        resources=("resources/authoring",),
    )

    def __init__(self, service: WhiteboardService, identity: Identity) -> None:
        self.tools: tuple[SemanticTool, ...] = (RenderWhiteboardTool(service, identity),)
        self._started = False

    async def initialize(self) -> None:
        self._started = True

    async def health(self) -> PluginHealth:
        return PluginHealth(self._started, None if self._started else "plugin is not initialized")

    async def stop(self) -> None:
        self._started = False
