from __future__ import annotations

from codex2lark.capabilities.artifacts.tools import (
    BaseService,
    CreateBaseTool,
    UpsertBaseRecordsTool,
)
from codex2lark.core.models import Identity
from codex2lark.runtime.plugins import PluginHealth, PluginManifest
from codex2lark.runtime.tools import SemanticTool


class FeishuBasePlugin:
    manifest = PluginManifest(
        plugin_id="feishu-base",
        version="1.0.0",
        runtime_api=1,
        capabilities=("base.app.create", "base.records.upsert"),
        required_scopes=(
            "base:app:create",
            "base:record:create",
            "base:record:update",
        ),
        resources=("resources/authoring",),
    )

    def __init__(self, service: BaseService, identity: Identity) -> None:
        self.tools: tuple[SemanticTool, ...] = (
            CreateBaseTool(service, identity),
            UpsertBaseRecordsTool(service, identity),
        )
        self._started = False

    async def initialize(self) -> None:
        self._started = True

    async def health(self) -> PluginHealth:
        return PluginHealth(self._started, None if self._started else "plugin is not initialized")

    async def stop(self) -> None:
        self._started = False
