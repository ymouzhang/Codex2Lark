from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class CapacityLane:
    tenant_key: str
    app_id: str
    scope_key: str

    def __post_init__(self) -> None:
        if not self.tenant_key or not self.app_id or not self.scope_key:
            raise ValueError("capacity lane identity is required")


@dataclass(frozen=True, slots=True)
class CapacitySnapshot:
    limit: int
    in_use: int
    queued: int
    queued_lanes: int


@dataclass(slots=True)
class _ResourceState:
    limit: int
    in_use: int = 0
    queues: dict[CapacityLane, deque[asyncio.Future[None]]] = field(default_factory=dict)
    round_robin: deque[CapacityLane] = field(default_factory=deque)


class FairCapacityGate:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._resources: dict[str, _ResourceState] = {}

    @asynccontextmanager
    async def capacity(
        self, resource_id: str, lane: CapacityLane, *, limit: int
    ) -> AsyncIterator[None]:
        if not resource_id or limit < 1:
            raise ValueError("capacity resource and positive limit are required")
        await self._acquire(resource_id, lane, limit=limit)
        try:
            yield
        finally:
            await self._release(resource_id)

    async def snapshot(self) -> dict[str, CapacitySnapshot]:
        async with self._lock:
            return {
                resource_id: CapacitySnapshot(
                    state.limit,
                    state.in_use,
                    sum(len(queue) for queue in state.queues.values()),
                    len(state.queues),
                )
                for resource_id, state in sorted(self._resources.items())
            }

    async def _acquire(self, resource_id: str, lane: CapacityLane, *, limit: int) -> None:
        future = asyncio.get_running_loop().create_future()
        async with self._lock:
            state = self._resources.get(resource_id)
            if state is None:
                state = _ResourceState(limit)
                self._resources[resource_id] = state
            elif state.limit != limit:
                raise ValueError(f"capacity limit changed for active resource: {resource_id}")
            queue = state.queues.get(lane)
            if queue is None:
                queue = deque()
                state.queues[lane] = queue
                state.round_robin.append(lane)
            queue.append(future)
            self._dispatch(state)
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            await self._cancel_waiter(resource_id, lane, future)
            raise

    async def _cancel_waiter(
        self,
        resource_id: str,
        lane: CapacityLane,
        future: asyncio.Future[None],
    ) -> None:
        async with self._lock:
            state = self._resources[resource_id]
            if future.done():
                state.in_use -= 1
                self._dispatch(state)
                return
            queue = state.queues.get(lane)
            if queue is not None:
                with suppress(ValueError):
                    queue.remove(future)
                if not queue:
                    del state.queues[lane]
                    self._remove_lane(state.round_robin, lane)

    async def _release(self, resource_id: str) -> None:
        async with self._lock:
            state = self._resources[resource_id]
            if state.in_use < 1:
                raise RuntimeError(f"capacity permit underflow: {resource_id}")
            state.in_use -= 1
            self._dispatch(state)

    @staticmethod
    def _dispatch(state: _ResourceState) -> None:
        while state.in_use < state.limit and state.round_robin:
            lane = state.round_robin.popleft()
            queue = state.queues[lane]
            future = queue.popleft()
            if queue:
                state.round_robin.append(lane)
            else:
                del state.queues[lane]
            if future.done():
                continue
            state.in_use += 1
            future.set_result(None)

    @staticmethod
    def _remove_lane(lanes: deque[CapacityLane], lane: CapacityLane) -> None:
        with suppress(ValueError):
            lanes.remove(lane)
