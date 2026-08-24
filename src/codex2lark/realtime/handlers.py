from __future__ import annotations

import asyncio
import logging

from ..adapters.lark_cli import safe_tool_call_error
from ..core.models import Identity
from ..services.chat_membership import ChatMembershipService
from .delivery import EventReference

logger = logging.getLogger(__name__)

BOT_ADDED_EVENT_KEY = "im.chat.member.bot.added_v1"


class BotAddedMembershipHandler:
    """Deterministically ensures the current user joins a newly bot-enabled group."""

    def __init__(
        self,
        membership: ChatMembershipService,
        *,
        retry_delays: tuple[float, ...] = (0.0, 0.5, 2.0),
    ) -> None:
        if not retry_delays:
            raise ValueError("at least one handler attempt is required")
        self.membership = membership
        self.retry_delays = retry_delays

    async def handle(self, reference: EventReference) -> None:
        if reference.event_type != BOT_ADDED_EVENT_KEY:
            logger.warning(
                "unsupported event skipped; event_id=%s event_type=%s",
                reference.event_id,
                reference.event_type,
            )
            return
        for attempt, delay in enumerate(self.retry_delays, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                result = await self.membership.ensure_current_user(
                    chat_id=reference.chat_id,
                    chat_identity=Identity.BOT,
                )
                logger.info(
                    "bot-added event handled; event_id=%s chat_id=%s status=%s",
                    reference.event_id,
                    reference.chat_id,
                    result.get("status", "unknown"),
                )
                return
            except Exception as exc:
                if attempt < len(self.retry_delays):
                    continue
                error = safe_tool_call_error(exc)["error"]
                logger.error(
                    "bot-added event failed; event_id=%s chat_id=%s category=%s message=%s",
                    reference.event_id,
                    reference.chat_id,
                    error["category"],
                    error["message"],
                )
