from __future__ import annotations

import pytest

from codex2lark.core.models import Identity
from codex2lark.realtime.delivery import EventReference
from codex2lark.realtime.handlers import BOT_ADDED_EVENT_KEY, BotAddedMembershipHandler


class FakeMembership:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls: list[tuple[str, object]] = []

    async def ensure_current_user(
        self, *, chat_id: str, chat_identity: object
    ) -> dict[str, object]:
        self.calls.append((chat_id, chat_identity))
        if len(self.calls) <= self.failures:
            raise RuntimeError("temporary failure")
        return {"status": "added"}


@pytest.mark.asyncio
async def test_bot_added_handler_retries_and_uses_bot_identity() -> None:
    membership = FakeMembership(failures=1)
    handler = BotAddedMembershipHandler(  # type: ignore[arg-type]
        membership, retry_delays=(0.0, 0.0)
    )

    await handler.handle(
        EventReference(event_id="evt_1", event_type=BOT_ADDED_EVENT_KEY, chat_id="oc_group")
    )

    assert membership.calls == [("oc_group", Identity.BOT), ("oc_group", Identity.BOT)]


@pytest.mark.asyncio
async def test_bot_added_handler_skips_unsupported_event() -> None:
    membership = FakeMembership()
    handler = BotAddedMembershipHandler(membership)  # type: ignore[arg-type]

    await handler.handle(EventReference(event_id="evt_1", event_type="other", chat_id="oc_group"))

    assert membership.calls == []
