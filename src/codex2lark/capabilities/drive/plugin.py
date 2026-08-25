from __future__ import annotations

from typing import Any, Protocol

from codex2lark.core.models import Identity
from codex2lark.runtime.plugins import PluginHealth, PluginManifest


class DriveService(Protocol):
    async def ensure_managed_folder(self, identity: Identity) -> dict[str, Any]: ...

    async def find_managed_folder(self, identity: Identity) -> dict[str, Any] | None: ...


class FeishuDrivePlugin:
    manifest = PluginManifest(
        plugin_id="feishu-drive",
        version="1.0.0",
        runtime_api=1,
        capabilities=("drive.managed-folder", "drive.metadata.search"),
        required_scopes=(
            "drive:drive.metadata:readonly",
            "drive:drive",
        ),
        resources=("resources/authoring",),
    )

    def __init__(self, service: DriveService) -> None:
        self.service = service
        self._started = False

    async def initialize(self) -> None:
        self._started = True

    async def health(self) -> PluginHealth:
        return PluginHealth(self._started, None if self._started else "plugin is not initialized")

    async def stop(self) -> None:
        self._started = False
