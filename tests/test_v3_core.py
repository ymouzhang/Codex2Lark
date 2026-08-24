from __future__ import annotations

import asyncio

import pytest

from codex2lark.core.budgets import BudgetKind, BudgetLedger, BudgetLimit
from codex2lark.core.cancellation import CancellationToken, CancelledByPolicyError
from codex2lark.runtime.plugins import (
    PluginHealth,
    PluginManager,
    PluginManifest,
    PluginState,
)


class FakePlugin:
    def __init__(self, plugin_id: str, capability: str, *, healthy: bool = True) -> None:
        self.manifest = PluginManifest(
            plugin_id=plugin_id,
            version="1.0.0",
            runtime_api=1,
            capabilities=(capability,),
            storage_namespace=plugin_id.replace("-", "_"),
        )
        self.healthy = healthy
        self.started = False
        self.stopped = False

    async def initialize(self) -> None:
        self.started = True

    async def health(self) -> PluginHealth:
        return PluginHealth(self.healthy, None if self.healthy else "test failure")

    async def stop(self) -> None:
        self.stopped = True


def test_budget_ledger_reserves_consumes_and_releases() -> None:
    ledger = BudgetLedger.from_limits([BudgetLimit(BudgetKind.TOOL_CALLS, 5)])

    ledger.reserve(BudgetKind.TOOL_CALLS, 3)
    ledger.consume(BudgetKind.TOOL_CALLS, 2, from_reservation=True)
    ledger.release(BudgetKind.TOOL_CALLS, 1)
    ledger.consume(BudgetKind.TOOL_CALLS, 2)

    assert ledger.consumed[BudgetKind.TOOL_CALLS] == 4
    assert ledger.available(BudgetKind.TOOL_CALLS) == 1
    with pytest.raises(ValueError, match="budget exceeded"):
        ledger.consume(BudgetKind.TOOL_CALLS, 2)


async def test_cancellation_token_preserves_first_reason() -> None:
    token = CancellationToken()
    token.cancel("user requested cancellation")
    token.cancel("later reason")

    assert await token.wait() == "user requested cancellation"
    with pytest.raises(CancelledByPolicyError, match="user requested"):
        token.raise_if_cancelled()


async def test_plugin_manager_starts_checks_and_stops_plugins() -> None:
    first = FakePlugin("feishu-docs", "docs.create")
    second = FakePlugin("feishu-im", "im.reply")
    manager = PluginManager(runtime_api=1, allowlist={"feishu-docs", "feishu-im"})
    manager.register(second)
    manager.register(first)

    await manager.start()
    manager.require_capabilities(["docs.create", "im.reply"])

    assert manager.snapshot() == {
        "feishu-im": (PluginState.READY, None),
        "feishu-docs": (PluginState.READY, None),
    }

    await manager.stop()
    assert first.stopped and second.stopped


async def test_plugin_manager_rolls_back_started_plugins_on_readiness_failure() -> None:
    first = FakePlugin("a-plugin", "a.read")
    failed = FakePlugin("b-plugin", "b.read", healthy=False)
    manager = PluginManager(runtime_api=1, allowlist={"a-plugin", "b-plugin"})
    manager.register(first)
    manager.register(failed)

    with pytest.raises(RuntimeError, match="failed readiness"):
        await manager.start()

    assert first.stopped
    assert manager.snapshot()["b-plugin"] == (PluginState.UNHEALTHY, "test failure")


def test_plugin_manager_rejects_untrusted_or_conflicting_plugins() -> None:
    manager = PluginManager(runtime_api=1, allowlist={"feishu-docs", "feishu-im"})
    manager.register(FakePlugin("feishu-docs", "docs.create"))

    with pytest.raises(ValueError, match="not allowlisted"):
        manager.register(FakePlugin("unknown", "unknown.read"))
    with pytest.raises(ValueError, match="duplicate capabilities"):
        manager.register(FakePlugin("feishu-im", "docs.create"))


async def test_cancellation_wait_blocks_until_cancelled() -> None:
    token = CancellationToken()
    waiter = asyncio.create_task(token.wait())
    await asyncio.sleep(0)
    assert not waiter.done()
    token.cancel("shutdown")
    assert await waiter == "shutdown"
