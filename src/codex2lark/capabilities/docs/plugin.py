from __future__ import annotations

from codex2lark.core.models import Identity
from codex2lark.runtime.plugins import PluginHealth, PluginManifest
from codex2lark.runtime.tools import SemanticTool

from .tools import DocumentService, document_tools


class FeishuDocsPlugin:
    manifest = PluginManifest(
        plugin_id="feishu-docs",
        version="1.0.0",
        runtime_api=1,
        capabilities=(
            "docs.document.search",
            "docs.document.inspect",
            "docs.document.create",
            "docs.document.edit",
        ),
        required_scopes=(
            "search:docs:read",
            "docx:document:readonly",
            "docx:document:write_only",
            "drive:drive.metadata:readonly",
        ),
        resources=("resources/authoring",),
    )

    def __init__(self, service: DocumentService, identity: Identity) -> None:
        self.tools: tuple[SemanticTool, ...] = tuple(document_tools(service, identity))
        self._started = False

    async def initialize(self) -> None:
        self._started = True

    async def health(self) -> PluginHealth:
        return PluginHealth(self._started, None if self._started else "plugin is not initialized")

    async def stop(self) -> None:
        self._started = False
