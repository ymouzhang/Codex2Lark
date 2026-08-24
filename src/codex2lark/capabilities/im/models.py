from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class IMAdmissionReason(StrEnum):
    ADMITTED = "admitted"
    NOT_GROUP = "not_group"
    BOT_SENDER = "bot_sender"
    BOT_NOT_MENTIONED = "bot_not_mentioned"
    EMPTY_REQUEST = "empty_request"
    ACCESS_REVOKED = "access_revoked"
    MALFORMED = "malformed"


@dataclass(frozen=True, slots=True)
class Mention:
    open_id: str
    display_name: str | None = None


@dataclass(frozen=True, slots=True)
class AttachmentReference:
    resource_key: str
    resource_type: str
    filename: str | None = None
    media_type: str | None = None
    declared_size: int | None = None

    def __post_init__(self) -> None:
        if not self.resource_key or not self.resource_type:
            raise ValueError("attachment resource identity is required")
        if self.declared_size is not None and self.declared_size < 0:
            raise ValueError("attachment size cannot be negative")


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    event_id: str
    tenant_key: str
    app_id: str
    chat_id: str
    chat_type: str
    message_id: str
    message_type: str
    sender_id: str
    sender_type: str
    body_text: str
    mentions: tuple[Mention, ...]
    attachments: tuple[AttachmentReference, ...]
    occurred_at_ms: int
    received_at_ms: int
    chat_name: str | None = None
    thread_id: str | None = None
    root_id: str | None = None
    parent_id: str | None = None
    sender_name: str | None = None
    updated_at_ms: int | None = None
    is_recalled: bool = False
    is_deleted: bool = False

    def __post_init__(self) -> None:
        required = (
            self.event_id,
            self.tenant_key,
            self.app_id,
            self.chat_id,
            self.message_id,
            self.message_type,
            self.sender_id,
            self.sender_type,
        )
        if any(not value.strip() for value in required):
            raise ValueError("message identity fields must be non-empty")

    @property
    def session_key(self) -> str:
        conversation = self.thread_id or self.root_id or self.message_id
        return "/".join((self.tenant_key, self.app_id, self.chat_id, conversation))

    def explicitly_mentions(self, bot_open_id: str) -> bool:
        return any(mention.open_id == bot_open_id for mention in self.mentions)

    @property
    def source_version_ms(self) -> int:
        return self.updated_at_ms or self.occurred_at_ms


@dataclass(frozen=True, slots=True)
class StoredMessage:
    tenant_key: str
    app_id: str
    chat_id: str
    message_id: str
    message_type: str
    sender_id: str
    sender_type: str
    body_text: str
    mentions: tuple[Mention, ...]
    occurred_at_ms: int
    source_version_ms: int
    thread_id: str | None = None
    root_id: str | None = None
    parent_id: str | None = None
    sender_name: str | None = None
    is_recalled: bool = False
    is_deleted: bool = False


@dataclass(frozen=True, slots=True)
class StoredAttachment:
    tenant_key: str
    app_id: str
    chat_id: str
    message_id: str
    resource_key: str
    resource_type: str
    filename: str | None
    media_type: str | None
    declared_size: int | None
    blob_id: str | None
    download_state: str
    parse_state: str
    parsed_content: str | None = None
    warning_code: str | None = None


@dataclass(frozen=True, slots=True)
class IMAdmissionDecision:
    reason: IMAdmissionReason
    task_id: str | None = None
    created: bool = False
    control_id: str | None = None

    @property
    def admitted(self) -> bool:
        return self.reason is IMAdmissionReason.ADMITTED
