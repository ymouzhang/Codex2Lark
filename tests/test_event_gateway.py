from __future__ import annotations

import pytest

from codex2lark.realtime.gateway import EventGateway


class LifecycleDouble:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True
        if self.fail_start:
            raise RuntimeError("start failed")

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_gateway_starts_dispatch_before_source_and_stops_both() -> None:
    source = LifecycleDouble()
    dispatcher = LifecycleDouble()
    gateway = EventGateway(source, dispatcher)  # type: ignore[arg-type]

    await gateway.start()
    await gateway.stop()

    assert source.started and source.stopped
    assert dispatcher.started and dispatcher.stopped


@pytest.mark.asyncio
async def test_gateway_cleans_dispatcher_when_source_start_fails() -> None:
    source = LifecycleDouble(fail_start=True)
    dispatcher = LifecycleDouble()
    gateway = EventGateway(source, dispatcher)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="start failed"):
        await gateway.start()

    assert dispatcher.stopped is True
