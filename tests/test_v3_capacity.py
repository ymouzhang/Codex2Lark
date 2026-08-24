from __future__ import annotations

import asyncio

import pytest

from codex2lark.runtime.capacity import CapacityLane, FairCapacityGate


async def test_capacity_gate_round_robins_lanes_without_exceeding_limit() -> None:
    gate = FairCapacityGate()
    lane_a = CapacityLane("tenant", "app", "group-a")
    lane_b = CapacityLane("tenant", "app", "group-b")
    holder_started = asyncio.Event()
    release_holder = asyncio.Event()
    order: list[str] = []
    active = 0
    maximum_active = 0

    async def holder() -> None:
        nonlocal active, maximum_active
        async with gate.capacity("provider:openai", lane_a, limit=1):
            active += 1
            maximum_active = max(maximum_active, active)
            order.append("holder")
            holder_started.set()
            await release_holder.wait()
            active -= 1

    async def queued(name: str, lane: CapacityLane) -> None:
        nonlocal active, maximum_active
        async with gate.capacity("provider:openai", lane, limit=1):
            active += 1
            maximum_active = max(maximum_active, active)
            order.append(name)
            await asyncio.sleep(0)
            active -= 1

    running_holder = asyncio.create_task(holder())
    await holder_started.wait()
    queued_tasks = (
        asyncio.create_task(queued("a-1", lane_a)),
        asyncio.create_task(queued("a-2", lane_a)),
        asyncio.create_task(queued("b-1", lane_b)),
    )
    await asyncio.sleep(0)
    release_holder.set()
    await asyncio.gather(running_holder, *queued_tasks)

    assert maximum_active == 1
    assert order == ["holder", "a-1", "b-1", "a-2"]
    assert (await gate.snapshot())["provider:openai"].queued == 0


async def test_capacity_gate_isolates_resources_and_releases_on_exception() -> None:
    gate = FairCapacityGate()
    lane = CapacityLane("tenant", "app", "group")
    both_started = asyncio.Event()
    active = 0

    async def use(resource: str) -> None:
        nonlocal active
        async with gate.capacity(resource, lane, limit=1):
            active += 1
            if active == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            active -= 1

    await asyncio.gather(use("plugin:docs"), use("plugin:sheets"))

    with pytest.raises(RuntimeError, match="boom"):
        async with gate.capacity("plugin:docs", lane, limit=1):
            raise RuntimeError("boom")

    snapshot = await gate.snapshot()
    assert snapshot["plugin:docs"].in_use == 0
    assert snapshot["plugin:sheets"].in_use == 0


async def test_cancelled_capacity_waiter_does_not_leak_permit() -> None:
    gate = FairCapacityGate()
    lane_a = CapacityLane("tenant", "app", "group-a")
    lane_b = CapacityLane("tenant", "app", "group-b")
    release = asyncio.Event()
    entered = asyncio.Event()

    async def holder() -> None:
        async with gate.capacity("provider:openai", lane_a, limit=1):
            entered.set()
            await release.wait()

    async def waiter() -> None:
        async with gate.capacity("provider:openai", lane_b, limit=1):
            raise AssertionError("cancelled waiter must not enter")

    holding = asyncio.create_task(holder())
    await entered.wait()
    waiting = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting
    release.set()
    await holding

    async with gate.capacity("provider:openai", lane_b, limit=1):
        pass

    snapshot = (await gate.snapshot())["provider:openai"]
    assert (snapshot.in_use, snapshot.queued, snapshot.queued_lanes) == (0, 0, 0)
