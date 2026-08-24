from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from codex2lark.runtime.context import ContextEvidence

from .attachments import AttachmentEvidence, AttachmentLoadRequest
from .models import IncomingMessage


@dataclass(frozen=True, slots=True)
class IMContextRequest:
    tenant_key: str
    app_id: str
    chat_id: str
    message_id: str

    def __post_init__(self) -> None:
        if any(
            not value for value in (self.tenant_key, self.app_id, self.chat_id, self.message_id)
        ):
            raise ValueError("IM context binding fields are required")


@dataclass(frozen=True, slots=True)
class MessagePage:
    messages: tuple[IncomingMessage, ...]
    complete: bool


@dataclass(frozen=True, slots=True)
class IMContextBundle:
    trigger: IncomingMessage
    evidence: tuple[ContextEvidence, ...]
    warnings: tuple[str, ...]


class LiveIMReader(Protocol):
    async def get_message(self, request: IMContextRequest) -> IncomingMessage: ...

    async def related_messages(self, trigger: IncomingMessage, *, limit: int) -> MessagePage: ...

    async def recent_messages(
        self, trigger: IncomingMessage, *, since_ms: int, limit: int
    ) -> MessagePage: ...


class MessageMirror(Protocol):
    async def upsert_message(self, message: IncomingMessage) -> bool: ...


class AttachmentEvidenceLoader(Protocol):
    async def load(self, request: AttachmentLoadRequest, *, now_ms: int) -> AttachmentEvidence: ...


class IMContextProvider:
    def __init__(
        self,
        source: LiveIMReader,
        mirror: MessageMirror,
        *,
        recent_limit: int = 30,
        related_limit: int = 50,
        lookback_ms: int = 2 * 60 * 60 * 1000,
        attachments: AttachmentEvidenceLoader | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if min(recent_limit, related_limit, lookback_ms) < 1:
            raise ValueError("IM context limits must be positive")
        self._source = source
        self._mirror = mirror
        self._recent_limit = recent_limit
        self._related_limit = related_limit
        self._lookback_ms = lookback_ms
        self._attachments = attachments
        self._clock_ms = clock_ms or (lambda: 0)

    async def collect(self, request: IMContextRequest) -> IMContextBundle:
        trigger = await self._source.get_message(request)
        self._validate_binding(request, trigger)
        if not await self._mirror.upsert_message(trigger):
            raise PermissionError("chat access was revoked")
        if trigger.is_recalled or trigger.is_deleted:
            raise LookupError("trigger message is no longer available")

        if trigger.thread_id or trigger.root_id or trigger.parent_id:
            page = await self._source.related_messages(trigger, limit=self._related_limit)
        else:
            page = await self._source.recent_messages(
                trigger,
                since_ms=max(0, trigger.occurred_at_ms - self._lookback_ms),
                limit=self._recent_limit,
            )

        selected: dict[str, IncomingMessage] = {}
        for message in page.messages:
            self._validate_binding(request, message)
            if not await self._mirror.upsert_message(message):
                raise PermissionError("chat access was revoked")
            if (
                message.message_id == trigger.message_id
                or message.is_recalled
                or message.is_deleted
            ):
                continue
            previous = selected.get(message.message_id)
            if previous is None or message.source_version_ms > previous.source_version_ms:
                selected[message.message_id] = message
        ordered = sorted(selected.values(), key=lambda item: (item.occurred_at_ms, item.message_id))
        evidence = [self._evidence(message) for message in ordered]
        warnings = [] if page.complete else ["im_context_incomplete"]
        if self._attachments is not None:
            for message in (*ordered, trigger):
                for attachment in message.attachments:
                    loaded = await self._attachments.load(
                        AttachmentLoadRequest(
                            message.tenant_key,
                            message.app_id,
                            message.chat_id,
                            message.message_id,
                            attachment.resource_key,
                        ),
                        now_ms=self._clock_ms(),
                    )
                    evidence.append(loaded.evidence)
                    if loaded.warning_code:
                        warnings.append(loaded.warning_code)
        return IMContextBundle(trigger, tuple(evidence), tuple(dict.fromkeys(warnings)))

    @staticmethod
    def _validate_binding(request: IMContextRequest, message: IncomingMessage) -> None:
        if (
            message.tenant_key != request.tenant_key
            or message.app_id != request.app_id
            or message.chat_id != request.chat_id
        ):
            raise PermissionError("live IM source returned a message outside the trusted binding")

    @staticmethod
    def _evidence(message: IncomingMessage) -> ContextEvidence:
        sender = message.sender_name or message.sender_id
        content = (
            f"time_ms={message.occurred_at_ms}\n"
            f"sender={sender}\n"
            f"message_type={message.message_type}\n"
            f"content={message.body_text}"
        )
        return ContextEvidence(
            source_ref=f"im.message:{message.message_id}",
            content=content,
            source_version=str(message.source_version_ms),
        )
