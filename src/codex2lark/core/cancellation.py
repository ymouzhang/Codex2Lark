from __future__ import annotations

import asyncio


class CancelledByPolicyError(asyncio.CancelledError):
    pass


class CancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason

    def cancel(self, reason: str) -> None:
        if not self._event.is_set():
            self._reason = reason
            self._event.set()

    async def wait(self) -> str:
        await self._event.wait()
        assert self._reason is not None
        return self._reason

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise CancelledByPolicyError(self._reason or "cancelled")
