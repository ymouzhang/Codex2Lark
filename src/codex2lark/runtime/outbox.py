from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from codex2lark.core.events import LeasedOutboxMessage


class OutboxPublisher(Protocol):
    async def publish(self, item: LeasedOutboxMessage) -> str: ...


class OutboxStore(Protocol):
    async def lease_outbox(
        self, *, worker_id: str, now_ms: int, lease_ms: int, limit: int = 10
    ) -> list[LeasedOutboxMessage]: ...

    async def mark_outbox_sent(
        self,
        outbox_id: str,
        *,
        worker_id: str,
        upstream_ref: str,
        now_ms: int,
    ) -> None: ...

    async def retry_outbox(
        self,
        outbox_id: str,
        *,
        worker_id: str,
        error_code: str,
        available_at_ms: int,
        now_ms: int,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class OutboxBatch:
    sent_ids: tuple[str, ...]
    retry_ids: tuple[str, ...]


class OutboxDispatcher:
    def __init__(
        self,
        store: OutboxStore,
        publishers: dict[str, OutboxPublisher],
        *,
        worker_id: str,
        lease_ms: int = 30_000,
        retry_delay_ms: int = 1_000,
    ) -> None:
        if not worker_id or lease_ms < 1 or retry_delay_ms < 0:
            raise ValueError("outbox worker configuration is invalid")
        self._store = store
        self._publishers = dict(publishers)
        self._worker_id = worker_id
        self._lease_ms = lease_ms
        self._retry_delay_ms = retry_delay_ms

    async def run_once(self, *, now_ms: int, limit: int = 10) -> OutboxBatch:
        items = await self._store.lease_outbox(
            worker_id=self._worker_id,
            now_ms=now_ms,
            lease_ms=self._lease_ms,
            limit=limit,
        )
        sent: list[str] = []
        retries: list[str] = []
        for item in items:
            publisher = self._publishers.get(item.publisher_id)
            try:
                if publisher is None:
                    raise LookupError("outbox publisher is unavailable")
                upstream_ref = await publisher.publish(item)
            except Exception as exc:
                await self._store.retry_outbox(
                    item.outbox_id,
                    worker_id=self._worker_id,
                    error_code=type(exc).__name__,
                    available_at_ms=now_ms + self._retry_delay_ms,
                    now_ms=now_ms,
                )
                retries.append(item.outbox_id)
                continue
            await self._store.mark_outbox_sent(
                item.outbox_id,
                worker_id=self._worker_id,
                upstream_ref=upstream_ref,
                now_ms=now_ms,
            )
            sent.append(item.outbox_id)
        return OutboxBatch(tuple(sent), tuple(retries))
