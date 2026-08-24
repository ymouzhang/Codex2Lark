from __future__ import annotations

from dataclasses import dataclass, field

from .models import IMAdmissionReason, IncomingMessage


@dataclass(frozen=True, slots=True)
class IMAdmissionPolicy:
    enabled_chat_ids: frozenset[str] = field(default_factory=frozenset)
    authorized_actor_ids: frozenset[str] = field(default_factory=frozenset)

    def evaluate(self, message: IncomingMessage) -> IMAdmissionReason:
        if self.enabled_chat_ids and message.chat_id not in self.enabled_chat_ids:
            return IMAdmissionReason.DISABLED_GROUP
        if self.authorized_actor_ids and message.sender_id not in self.authorized_actor_ids:
            return IMAdmissionReason.UNAUTHORIZED_ACTOR
        return IMAdmissionReason.ADMITTED
