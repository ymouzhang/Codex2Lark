from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class PluginState(StrEnum):
    DISCOVERED = "discovered"
    READY = "ready"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"


@dataclass(frozen=True, slots=True)
class PluginManifest:
    plugin_id: str
    version: str
    runtime_api: int
    capabilities: tuple[str, ...]
    events: tuple[str, ...] = ()
    required_scopes: tuple[str, ...] = ()
    storage_namespace: str = ""
    resources: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.plugin_id or self.plugin_id.lower() != self.plugin_id:
            raise ValueError("plugin_id must be non-empty lowercase text")
        if self.runtime_api < 1:
            raise ValueError("runtime_api must be positive")
        if not self.capabilities:
            raise ValueError("plugin must declare at least one capability")
        if len(set(self.capabilities)) != len(self.capabilities):
            raise ValueError("plugin capabilities must be unique")


@dataclass(frozen=True, slots=True)
class PluginHealth:
    healthy: bool
    reason: str | None = None


class CapabilityPlugin(Protocol):
    manifest: PluginManifest

    async def initialize(self) -> None: ...

    async def health(self) -> PluginHealth: ...

    async def stop(self) -> None: ...


@dataclass(slots=True)
class ManagedPlugin:
    plugin: CapabilityPlugin
    state: PluginState = PluginState.DISCOVERED
    reason: str | None = None


class PluginManager:
    def __init__(self, *, runtime_api: int, allowlist: set[str]) -> None:
        self._runtime_api = runtime_api
        self._allowlist = frozenset(allowlist)
        self._plugins: dict[str, ManagedPlugin] = {}

    def register(self, plugin: CapabilityPlugin) -> None:
        manifest = plugin.manifest
        if manifest.plugin_id not in self._allowlist:
            raise ValueError(f"plugin is not allowlisted: {manifest.plugin_id}")
        if manifest.runtime_api != self._runtime_api:
            raise ValueError(
                f"plugin {manifest.plugin_id} requires runtime API {manifest.runtime_api}"
            )
        if manifest.plugin_id in self._plugins:
            raise ValueError(f"duplicate plugin id: {manifest.plugin_id}")
        claimed = set(manifest.capabilities)
        for managed in self._plugins.values():
            overlap = claimed.intersection(managed.plugin.manifest.capabilities)
            if overlap:
                raise ValueError(f"duplicate capabilities: {sorted(overlap)}")
        self._plugins[manifest.plugin_id] = ManagedPlugin(plugin=plugin)

    async def start(self) -> None:
        initialized: list[ManagedPlugin] = []
        try:
            for plugin_id in sorted(self._plugins):
                managed = self._plugins[plugin_id]
                await managed.plugin.initialize()
                health = await managed.plugin.health()
                if not health.healthy:
                    managed.state = PluginState.UNHEALTHY
                    managed.reason = health.reason
                    raise RuntimeError(
                        f"plugin failed readiness: {plugin_id}: {health.reason or 'unknown'}"
                    )
                managed.state = PluginState.READY
                initialized.append(managed)
        except BaseException:
            for managed in reversed(initialized):
                await managed.plugin.stop()
                managed.state = PluginState.STOPPED
            raise

    async def refresh_health(self) -> dict[str, PluginHealth]:
        result: dict[str, PluginHealth] = {}
        for plugin_id, managed in self._plugins.items():
            health = await managed.plugin.health()
            managed.state = PluginState.READY if health.healthy else PluginState.UNHEALTHY
            managed.reason = health.reason
            result[plugin_id] = health
        return result

    async def stop(self) -> None:
        for plugin_id in reversed(sorted(self._plugins)):
            managed = self._plugins[plugin_id]
            if managed.state in (PluginState.READY, PluginState.UNHEALTHY):
                await managed.plugin.stop()
                managed.state = PluginState.STOPPED

    def require_capabilities(self, capabilities: Sequence[str]) -> None:
        providers = {
            capability: managed
            for managed in self._plugins.values()
            for capability in managed.plugin.manifest.capabilities
        }
        for capability in capabilities:
            managed = providers.get(capability)
            if managed is None:
                raise LookupError(f"capability is unavailable: {capability}")
            if managed.state is not PluginState.READY:
                raise RuntimeError(f"capability provider is not ready: {capability}")

    def snapshot(self) -> dict[str, tuple[PluginState, str | None]]:
        return {
            plugin_id: (managed.state, managed.reason)
            for plugin_id, managed in self._plugins.items()
        }


PluginFactory = Callable[[], CapabilityPlugin]
AsyncPluginFactory = Callable[[], Awaitable[CapabilityPlugin]]
