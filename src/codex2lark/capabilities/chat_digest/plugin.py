from __future__ import annotations

from codex2lark.core.models import Identity
from codex2lark.runtime.plugins import PluginHealth, PluginManifest
from codex2lark.runtime.tools import SemanticTool

from .tools import ChatDigestService, chat_digest_tools


class FeishuChatDigestPlugin:
    manifest = PluginManifest(
        plugin_id="feishu-chat-digest",
        version="1.1.0",
        runtime_api=1,
        capabilities=("im.chat.digest.publish",),
        required_scopes=(
            "im:message:readonly",
            "im:resource",
            "im:chat:readonly",
            "docx:document:readonly",
            "docx:document:write_only",
            "drive:drive.metadata:readonly",
        ),
        resources=("resources/chat-digest",),
    )

    def __init__(self, service: ChatDigestService, identity: Identity) -> None:
        self.tools: tuple[SemanticTool, ...] = tuple(chat_digest_tools(service, identity))
        self._started = False

    async def initialize(self) -> None:
        self._started = True

    async def health(self) -> PluginHealth:
        return PluginHealth(self._started, None if self._started else "plugin is not initialized")

    async def stop(self) -> None:
        self._started = False
