from __future__ import annotations

from codex2lark.runtime.plugins import PluginHealth, PluginManifest


class FeishuIMPlugin:
    manifest = PluginManifest(
        plugin_id="feishu-im",
        version="1.0.0",
        runtime_api=1,
        capabilities=(
            "im.group_message.receive",
            "im.message.reply",
            "im.thread.read",
            "im.attachment.read",
        ),
        events=("im.message.receive_v1", "im.chat.member.bot.added_v1"),
        required_scopes=(
            "im:message.group_at_msg:readonly",
            "im:message:readonly",
            "im:message:send_as_bot",
        ),
        storage_namespace="im",
        resources=("resources/im",),
    )

    def __init__(self) -> None:
        self._started = False

    async def initialize(self) -> None:
        self._started = True

    async def health(self) -> PluginHealth:
        return PluginHealth(self._started, None if self._started else "plugin is not initialized")

    async def stop(self) -> None:
        self._started = False


def create_plugin() -> FeishuIMPlugin:
    return FeishuIMPlugin()
