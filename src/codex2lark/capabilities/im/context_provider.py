from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from codex2lark.runtime.context import ContextEvidence

from .attachments import AttachmentEvidence, AttachmentLoadRequest
from .models import IncomingMessage, StoredAttachment

_FILENAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._@+~-]*\.[A-Za-z0-9][A-Za-z0-9+~-]{0,15}")
_ATTACHMENT_INTENT_TERMS = ("文件", "附件", "attachment", "file")


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


class IMHistoryUnavailableError(PermissionError):
    """The verified trigger is readable but optional group history is not authorized."""


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

    async def recent_attachments(
        self,
        tenant_key: str,
        app_id: str,
        chat_id: str,
        *,
        since_ms: int,
        before_ms: int,
        limit: int = 100,
    ) -> list[StoredAttachment]: ...


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
        attachment_search_limit: int = 500,
        attachment_lookback_ms: int = 30 * 24 * 60 * 60 * 1000,
        attachments: AttachmentEvidenceLoader | None = None,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        if (
            min(
                recent_limit,
                related_limit,
                lookback_ms,
                attachment_search_limit,
                attachment_lookback_ms,
            )
            < 1
        ):
            raise ValueError("IM context limits must be positive")
        self._source = source
        self._mirror = mirror
        self._recent_limit = recent_limit
        self._related_limit = related_limit
        self._lookback_ms = lookback_ms
        self._attachment_search_limit = attachment_search_limit
        self._attachment_lookback_ms = attachment_lookback_ms
        self._attachments = attachments
        self._clock_ms = clock_ms or (lambda: 0)

    async def collect(self, request: IMContextRequest) -> IMContextBundle:
        trigger = await self._source.get_message(request)
        self._validate_binding(request, trigger)
        if not await self._mirror.upsert_message(trigger):
            raise PermissionError("chat access was revoked")
        if trigger.is_recalled or trigger.is_deleted:
            raise LookupError("trigger message is no longer available")

        warnings: list[str] = []
        related = bool(trigger.thread_id or trigger.root_id or trigger.parent_id)
        attachment_search = not related and self._requests_attachment_search(trigger.body_text)
        lookback_ms = self._attachment_lookback_ms if attachment_search else self._lookback_ms
        recent_limit = self._attachment_search_limit if attachment_search else self._recent_limit
        since_ms = max(0, trigger.occurred_at_ms - lookback_ms)
        try:
            if related:
                page = await self._source.related_messages(trigger, limit=self._related_limit)
            else:
                page = await self._source.recent_messages(
                    trigger,
                    since_ms=since_ms,
                    limit=recent_limit,
                )
        except IMHistoryUnavailableError:
            warnings.append("im_context_history_unavailable")
            recovered = await self._recover_observed_attachments(
                request,
                trigger,
                since_ms=since_ms,
                limit=recent_limit,
                warnings=warnings,
            )
            page = MessagePage(recovered, False)

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
        if not page.complete:
            warnings.append("im_context_incomplete")
        if self._attachments is not None:
            planned = self._plan_attachments(trigger, ordered, related=related, warnings=warnings)
            for message, resource_keys in planned:
                for attachment in message.attachments:
                    if attachment.resource_key not in resource_keys:
                        continue
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

    @classmethod
    def _plan_attachments(
        cls,
        trigger: IncomingMessage,
        contextual: list[IncomingMessage],
        *,
        related: bool,
        warnings: list[str],
    ) -> tuple[tuple[IncomingMessage, frozenset[str]], ...]:
        planned: list[tuple[IncomingMessage, frozenset[str]]] = []
        if trigger.attachments:
            planned.append((trigger, frozenset(item.resource_key for item in trigger.attachments)))
        if related:
            planned.extend(
                (message, frozenset(item.resource_key for item in message.attachments))
                for message in contextual
                if message.attachments
            )
            return tuple(planned)

        request = cls._normalized_filename_text(trigger.body_text)
        candidates: dict[str, list[tuple[IncomingMessage, str]]] = {}
        for message in contextual:
            for attachment in message.attachments:
                filename = cls._normalized_filename_text(attachment.filename or "")
                if filename and filename in request:
                    candidates.setdefault(filename, []).append((message, attachment.resource_key))
        for matches in candidates.values():
            if len(matches) != 1:
                warnings.append("im_attachment_ambiguous")
                continue
            message, resource_key = matches[0]
            planned.append((message, frozenset({resource_key})))
        return tuple(planned)

    @staticmethod
    def _normalized_filename_text(value: str) -> str:
        return unicodedata.normalize("NFKC", value).casefold()

    @classmethod
    def _requests_attachment_search(cls, value: str) -> bool:
        normalized = cls._normalized_filename_text(value)
        return bool(_FILENAME_PATTERN.search(normalized)) or any(
            term in normalized for term in _ATTACHMENT_INTENT_TERMS
        )

    async def _recover_observed_attachments(
        self,
        request: IMContextRequest,
        trigger: IncomingMessage,
        *,
        since_ms: int,
        limit: int,
        warnings: list[str],
    ) -> tuple[IncomingMessage, ...]:
        observed = await self._mirror.recent_attachments(
            request.tenant_key,
            request.app_id,
            request.chat_id,
            since_ms=since_ms,
            before_ms=trigger.occurred_at_ms,
            limit=limit,
        )
        request_text = self._normalized_filename_text(trigger.body_text)
        matches: dict[str, list[StoredAttachment]] = {}
        for attachment in observed:
            filename = self._normalized_filename_text(attachment.filename or "")
            if filename and filename in request_text:
                matches.setdefault(filename, []).append(attachment)

        recovered: list[IncomingMessage] = []
        for candidates in matches.values():
            identities = {(item.message_id, item.resource_key) for item in candidates}
            if len(identities) != 1:
                warnings.append("im_attachment_ambiguous")
                continue
            candidate = candidates[0]
            live = await self._source.get_message(
                IMContextRequest(
                    request.tenant_key,
                    request.app_id,
                    request.chat_id,
                    candidate.message_id,
                )
            )
            self._validate_binding(request, live)
            live_names = {
                self._normalized_filename_text(item.filename or "") for item in live.attachments
            }
            expected = self._normalized_filename_text(candidate.filename or "")
            if expected not in live_names:
                warnings.append("im_attachment_live_mismatch")
                continue
            recovered.append(live)
        return tuple(recovered)

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
