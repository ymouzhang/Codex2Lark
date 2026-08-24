from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape, unescape
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..adapters.lark_cli import LarkCli, safe_tool_call_error
from ..authoring.compiler import preflight_content
from ..authoring.verifier import (
    extract_content,
    extract_resource,
    find_first_value,
    verify_document,
)
from ..core.errors import (
    AmbiguityError,
    Codex2LarkError,
    ConflictError,
    ErrorCategory,
    NotFoundError,
)
from ..core.models import (
    ChatDigestRequest,
    DetailLevel,
    DocumentFormat,
    Identity,
    InspectDocumentRequest,
    ResourceRef,
)
from ..core.runtime import EphemeralWorkspace
from .chat_membership import ChatMembershipService
from .docs import DocsService
from .drive import DriveService
from .notification import NotificationService

_IMAGE_KEY = re.compile(r"img_[A-Za-z0-9_-]+")
_IMAGE_MARKER = re.compile(r"!\[[^\]]*\]\(img_[^)]+\)")
_RESOURCE_TAG = re.compile(r"<(?:file|audio|video|media)\b[^>]*/?>", re.IGNORECASE)
_RESOURCE_NAME = re.compile(
    r'<(?:file|audio|video|media)\b[^>]*\b(?:name|file_name|filename)="([^"]+)"',
    re.IGNORECASE,
)
_FILE_NAME_KEYS = {"file_name", "filename", "name"}


@dataclass(frozen=True, slots=True)
class ChatEntry:
    message_id: str
    create_time: str
    timestamp: float | None
    sender: str
    text: str
    image_keys: tuple[str, ...]
    file_names: tuple[str, ...]
    message_type: str
    thread_reply: bool = False


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _validation(message: str, **details: Any) -> Codex2LarkError:
    return Codex2LarkError(ErrorCategory.VALIDATION, message, details=details)


def _decoded_content(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _collect_text(value: Any) -> list[str]:
    texts: list[str] = []
    if isinstance(value, str):
        cleaned = _RESOURCE_TAG.sub("", _IMAGE_MARKER.sub("", value)).strip()
        if cleaned and not cleaned.startswith(("img_", "file_")):
            texts.append(cleaned)
        return texts
    if isinstance(value, list):
        for child in value:
            texts.extend(_collect_text(child))
        return texts
    if not isinstance(value, dict):
        return texts

    for key in ("title", "text"):
        text = value.get(key)
        if isinstance(text, str) and text:
            texts.append(text)
    for key in ("content", "elements", "children", "items", "messages", "message_list"):
        child = value.get(key)
        if child is not None:
            texts.extend(_collect_text(child))
    for locale in ("zh_cn", "en_us"):
        child = value.get(locale)
        if child is not None:
            texts.extend(_collect_text(child))
    return texts


def _collect_image_keys(value: Any) -> list[str]:
    keys: list[str] = []
    if isinstance(value, str):
        keys.extend(_IMAGE_KEY.findall(value))
    elif isinstance(value, list):
        for child in value:
            keys.extend(_collect_image_keys(child))
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in {"image_key", "img_key"} and isinstance(child, str):
                keys.extend(_IMAGE_KEY.findall(child))
            elif key in {
                "content",
                "elements",
                "children",
                "items",
                "messages",
                "message_list",
                "zh_cn",
                "en_us",
            }:
                keys.extend(_collect_image_keys(child))
    return list(dict.fromkeys(keys))


def _collect_file_names(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, str):
        names.extend(unescape(name) for name in _RESOURCE_NAME.findall(value))
    elif isinstance(value, list):
        for child in value:
            names.extend(_collect_file_names(child))
    elif isinstance(value, dict):
        has_file_key = any(key in value for key in ("file_key", "file_token"))
        if has_file_key:
            for key in _FILE_NAME_KEYS:
                name = value.get(key)
                if isinstance(name, str) and name:
                    names.append(name)
                    break
        for key in (
            "content",
            "elements",
            "children",
            "items",
            "messages",
            "message_list",
            "zh_cn",
            "en_us",
        ):
            child = value.get(key)
            if child is not None:
                names.extend(_collect_file_names(child))
    return list(dict.fromkeys(names))


def _timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        number = float(value)
    else:
        return None
    return number / 1000 if number > 10_000_000_000 else number


def _sender_name(message: dict[str, Any]) -> str:
    sender = message.get("sender")
    if isinstance(sender, dict):
        for key in ("name", "sender_name", "id", "sender_id"):
            value = sender.get(key)
            if isinstance(value, str) and value:
                return value
    return "系统"


def _normal_text(message_type: str, decoded: Any, file_names: list[str]) -> str:
    if message_type == "file":
        names = file_names or ["文件名未知"]
        return "\n".join(f"文件: {name} (未下载)" for name in names)
    if message_type in {"audio", "video", "media"}:
        label = {"audio": "音频", "video": "视频", "media": "媒体"}[message_type]
        names = file_names or ["文件名未知"]
        return "\n".join(f"{label}: {name} (未下载)" for name in names)
    if message_type == "sticker":
        return "贴纸消息 (未下载)"

    text = "\n".join(part.strip() for part in _collect_text(decoded) if part.strip())
    if file_names:
        file_lines = [f"文件: {name} (未下载)" for name in file_names]
        text = "\n".join([text, *file_lines]) if text else "\n".join(file_lines)
    if text:
        return text
    if message_type == "image":
        return "图片"
    if message_type == "interactive":
        return "交互卡片 (未展开)"
    if message_type == "system":
        return "系统消息"
    return f"{message_type or '未知'} 消息"


def _normalize_message(message: dict[str, Any], *, thread_reply: bool = False) -> ChatEntry:
    raw_content = message.get("content", "")
    decoded = _decoded_content(raw_content)
    message_type = str(message.get("msg_type") or "unknown").lower()
    file_names = _collect_file_names(decoded)
    image_keys = (
        _collect_image_keys(decoded) if message_type in {"image", "post", "merge_forward"} else []
    )
    deleted = message.get("deleted") is True
    text = "消息已撤回" if deleted else _normal_text(message_type, decoded, file_names)
    create_time_value = message.get("create_time")
    create_time = str(create_time_value) if create_time_value is not None else ""
    message_id_value = message.get("message_id")
    message_id = str(message_id_value) if message_id_value is not None else ""
    return ChatEntry(
        message_id=message_id,
        create_time=create_time,
        timestamp=_timestamp(create_time_value),
        sender=_sender_name(message),
        text=text,
        image_keys=tuple(image_keys if not deleted else []),
        file_names=tuple(file_names),
        message_type=message_type,
        thread_reply=thread_reply,
    )


def _flatten_messages(messages: list[Any]) -> list[ChatEntry]:
    entries: list[ChatEntry] = []

    def append(raw: Any, *, thread_reply: bool = False) -> None:
        if not isinstance(raw, dict):
            return
        entries.append(_normalize_message(raw, thread_reply=thread_reply))
        replies = raw.get("thread_replies")
        if isinstance(replies, list):
            for reply in replies:
                append(reply, thread_reply=True)

    for message in messages:
        append(message)

    unique: dict[str, ChatEntry] = {}
    anonymous = 0
    for entry in entries:
        key = entry.message_id
        if not key:
            anonymous += 1
            key = f"anonymous-{anonymous}"
        unique.setdefault(key, entry)
    return sorted(
        unique.values(),
        key=lambda item: (
            item.timestamp is None,
            item.timestamp if item.timestamp is not None else 0,
            item.message_id,
        ),
    )


def _render_time(entry: ChatEntry, zone: ZoneInfo) -> tuple[str, str]:
    if entry.timestamp is None:
        return "日期未知", entry.create_time or "时间未知"
    moment = datetime.fromtimestamp(entry.timestamp, tz=UTC).astimezone(zone)
    return moment.strftime("%Y-%m-%d"), moment.strftime("%H:%M:%S")


def _render_digest_xml(
    *,
    chat_name: str,
    start: str,
    end: str,
    entries: list[ChatEntry],
    image_paths: dict[tuple[str, str], str | None],
    zone: ZoneInfo,
) -> str:
    parts = [
        f"<title>{escape(chat_name)}</title>",
        '<callout background-color="light-blue" border-color="blue">',
        f"<p><b>群聊:</b> {escape(chat_name)}</p>",
        f"<p><b>时间范围:</b> {escape(start)} 至 {escape(end)}</p>",
        f"<p><b>消息数量:</b> {len(entries)}</p>",
        "</callout>",
        "<h1>群聊记录</h1>",
    ]
    if not entries:
        parts.append("<p>该时间范围内没有消息。</p>")
        return "".join(parts)

    active_date: str | None = None
    for entry in entries:
        date_label, time_label = _render_time(entry, zone)
        if date_label != active_date:
            parts.append(f"<h2>{escape(date_label)}</h2>")
            active_date = date_label
        reply = " · 话题回复" if entry.thread_reply else ""
        parts.append(f"<p><b>{escape(time_label)} · {escape(entry.sender)}{reply}</b></p>")
        for line in entry.text.splitlines() or [entry.text]:
            parts.append(f"<p>{escape(line)}</p>")
        for image_key in entry.image_keys:
            path = image_paths.get((entry.message_id, image_key))
            if path is None:
                parts.append(f"<p>图片无法读取: {escape(image_key)}</p>")
            else:
                caption = f"{entry.sender} · {time_label}"
                parts.append(
                    f'<img path="{escape(path, quote=True)}" '
                    f'caption="{escape(caption, quote=True)}"/>'
                )
    return "".join(parts)


class ChatDigestService:
    def __init__(
        self,
        lark: LarkCli,
        docs: DocsService,
        drive: DriveService,
        notifier: NotificationService | None = None,
        membership: ChatMembershipService | None = None,
    ) -> None:
        self.lark = lark
        self.docs = docs
        self.drive = drive
        self.notifier = notifier or NotificationService(lark)
        self.membership = membership or ChatMembershipService(lark)

    async def publish(self, request: ChatDigestRequest) -> dict[str, Any]:
        try:
            zone = ZoneInfo(request.timezone)
        except ZoneInfoNotFoundError as exc:
            raise _validation("unknown IANA timezone", timezone=request.timezone) from exc

        author_identity = Identity.USER
        chat = {
            **(await self._resolve_chat(request)),
            "identity": request.identity.value,
        }
        membership = await self.membership.ensure_current_user(
            chat_id=chat["chat_id"], chat_identity=request.identity
        )
        pulled = await self.lark.execute(
            [
                "im",
                "+chat-messages-list",
                "--chat-id",
                chat["chat_id"],
                "--start",
                request.start,
                "--end",
                request.end,
                "--order",
                "asc",
                "--page-size",
                "50",
                "--page-all",
                "--page-limit",
                str(request.page_limit),
                "--no-reactions",
                "--as",
                request.identity.value,
                "--format",
                "json",
            ]
        )
        pagination = pulled.meta.get("pagination")
        incomplete_meta = isinstance(pagination, dict) and pagination.get("complete") is False
        if pulled.data.get("has_more") is True or incomplete_meta:
            raise _validation(
                "group history pagination was incomplete; no digest was created",
                page_limit=request.page_limit,
            )
        raw_messages = pulled.data.get("messages", [])
        if not isinstance(raw_messages, list):
            raise _validation("lark-cli returned an invalid group message list")
        entries = _flatten_messages(raw_messages)
        if len(entries) > request.max_messages:
            raise _validation(
                "group history exceeded the declared message limit; no digest was created",
                actual=len(entries),
                max_messages=request.max_messages,
            )
        image_refs = [(entry.message_id, key) for entry in entries for key in entry.image_keys]
        if len(image_refs) > request.max_images:
            raise _validation(
                "group history exceeded the declared image limit; no digest was created",
                actual=len(image_refs),
                max_images=request.max_images,
            )

        existing, managed_folder, expected_revision = await self._existing_digest(
            chat_name=chat["name"],
            identity=author_identity,
        )

        warnings = list(pulled.warnings)
        with EphemeralWorkspace(max_file_bytes=8_000_000) as workspace:
            assert workspace.path is not None
            image_paths: dict[tuple[str, str], str | None] = {}
            for index, (message_id, image_key) in enumerate(image_refs):
                try:
                    downloaded = await self.lark.execute(
                        [
                            "im",
                            "+messages-resources-download",
                            "--message-id",
                            message_id,
                            "--file-key",
                            image_key,
                            "--type",
                            "image",
                            "--output",
                            f"./chat-image-{index}",
                            "--as",
                            request.identity.value,
                            "--format",
                            "json",
                        ],
                        cwd=workspace.path,
                    )
                    image_path = self._downloaded_path(downloaded.data, workspace.path, index)
                    image_paths[(message_id, image_key)] = workspace.relative_reference(image_path)
                    warnings.extend(downloaded.warnings)
                except Exception:
                    image_paths[(message_id, image_key)] = None
                    warnings.append(
                        f"image {image_key} from message {message_id} could not be inserted"
                    )

            xml = _render_digest_xml(
                chat_name=chat["name"],
                start=request.start,
                end=request.end,
                entries=entries,
                image_paths=image_paths,
                zone=zone,
            )
            preflight_content(xml, DocumentFormat.XML)
            content_path = workspace.write_text("chat-digest.xml", xml)
            if existing is None:
                if managed_folder is None:
                    managed_folder = await self.drive.ensure_managed_folder(author_identity)
                written = await self.lark.execute(
                    [
                        "docs",
                        "+create",
                        "--doc-format",
                        "xml",
                        "--title",
                        chat["name"],
                        "--content",
                        workspace.relative_reference(content_path),
                        "--parent-token",
                        managed_folder["token"],
                        "--as",
                        author_identity.value,
                        "--format",
                        "json",
                    ],
                    cwd=workspace.path,
                )
                action = "created"
                resource = extract_resource(written.data)
                reference = (
                    resource.get("url") or resource.get("document_id") or resource.get("token")
                )
            else:
                target = self._resource_ref(existing)
                live_before_write = await self.docs.inspect(
                    InspectDocumentRequest(
                        resource=target,
                        format=DocumentFormat.XML,
                        detail=DetailLevel.FULL,
                        identity=author_identity,
                    )
                )
                if (
                    expected_revision is not None
                    and live_before_write["revision"] != expected_revision
                ):
                    raise ConflictError(
                        "the managed group digest changed before refresh",
                        details={
                            "expected_revision": expected_revision,
                            "current_revision": live_before_write["revision"],
                        },
                    )
                if "群聊记录" not in extract_content(live_before_write["data"]):
                    raise ConflictError(
                        "the managed document is no longer a recognized group digest"
                    )
                written = await self.lark.execute(
                    [
                        "docs",
                        "+update",
                        "--doc",
                        target.value,
                        "--command",
                        "overwrite",
                        "--doc-format",
                        "xml",
                        "--content",
                        workspace.relative_reference(content_path),
                        "--as",
                        author_identity.value,
                        "--format",
                        "json",
                    ],
                    cwd=workspace.path,
                )
                action = "updated"
                resource = existing
                reference = target.value

        if not isinstance(reference, str):
            raise _validation("created digest did not contain a usable URL or token")
        inspected = await self.docs.inspect(
            InspectDocumentRequest(
                resource=(
                    ResourceRef(url=reference)
                    if reference.startswith("http")
                    else ResourceRef(token=reference)
                ),
                format=DocumentFormat.XML,
                detail=DetailLevel.FULL,
                identity=author_identity,
            )
        )
        policy = request.verification.model_copy(
            update={
                "expected_title": request.verification.expected_title or chat["name"],
                "required_text": [
                    *request.verification.required_text,
                    "群聊记录",
                ],
            }
        )
        verification = verify_document(inspected["data"], policy)
        if verification.status != "passed":
            raise Codex2LarkError(
                ErrorCategory.VERIFICATION,
                "group-chat digest was created but read-back verification failed",
                details={"resource": resource, "verification": verification.as_dict()},
            )
        live_resource = extract_resource(inspected["data"])
        warnings.extend(written.warnings)
        warnings.extend(inspected.get("warnings", []))
        if request.verification.fail_on_warning and warnings:
            raise Codex2LarkError(
                ErrorCategory.VERIFICATION,
                "group-chat digest was created but warnings are forbidden",
                details={"resource": resource, "warnings": warnings},
            )
        if action == "updated":
            try:
                notification = await self.notifier.document_edited(
                    resource=live_resource,
                    change_summary=(
                        f"刷新群聊“{chat['name']}”从 {request.start} 至 {request.end} "
                        f"的消息汇总, 共 {len(entries)} 条消息"
                    ),
                    revision=inspected["revision"],
                    operations_applied=1,
                )
            except Exception as exc:
                notification = {
                    "status": "failed",
                    "error": safe_tool_call_error(exc)["error"],
                }
                warnings.append(
                    "group digest refresh was verified, but the completion notification "
                    "failed; do not repeat the refresh solely to resend the message"
                )
        else:
            notification = {"status": "not_applicable", "reason": "document_created"}
        file_count = sum(len(entry.file_names) for entry in entries) + sum(
            1 for entry in entries if entry.message_type == "file" and not entry.file_names
        )
        inserted_images = sum(path is not None for path in image_paths.values())
        return {
            "ok": True,
            "action": action,
            "resource": live_resource,
            "managed_folder": managed_folder,
            "chat": chat,
            "author_identity": author_identity.value,
            "membership": membership,
            "range": {"start": request.start, "end": request.end, "timezone": request.timezone},
            "stats": {
                "messages": len(entries),
                "images_found": len(image_refs),
                "images_inserted": inserted_images,
                "files_listed": file_count,
            },
            "verification": verification.as_dict(),
            "notification": notification,
            "warnings": warnings,
        }

    async def _existing_digest(
        self, *, chat_name: str, identity: Identity
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int | None]:
        found = await self.drive.search_documents(chat_name, identity)
        managed_folder = found.get("managed_folder")
        folder = managed_folder if isinstance(managed_folder, dict) else None
        if found.get("scope") != "managed_folder":
            return None, folder, None
        matches = found.get("matches", [])
        if not isinstance(matches, list):
            raise _validation("managed digest search returned invalid candidates")
        if len(matches) > 1:
            raise AmbiguityError(
                "more than one managed group digest matched the exact group name",
                details={"chat_name": chat_name, "candidates": matches},
            )
        if not matches:
            return None, folder, None
        candidate = matches[0]
        if not isinstance(candidate, dict):
            raise _validation("managed digest candidate was invalid")
        inspected = await self.docs.inspect(
            InspectDocumentRequest(
                resource=self._resource_ref(candidate),
                format=DocumentFormat.XML,
                detail=DetailLevel.FULL,
                identity=identity,
            )
        )
        if "群聊记录" not in extract_content(inspected["data"]):
            raise ConflictError(
                "a managed same-title document exists but is not a recognized group digest",
                details={"chat_name": chat_name, "candidate": candidate},
            )
        return candidate, folder, inspected["revision"]

    @staticmethod
    def _resource_ref(resource: dict[str, Any]) -> ResourceRef:
        url = resource.get("url")
        token = resource.get("document_id") or resource.get("token")
        if isinstance(url, str):
            return ResourceRef(url=url)
        if isinstance(token, str):
            return ResourceRef(token=token)
        raise _validation("group digest candidate did not contain a usable URL or token")

    async def _resolve_chat(self, request: ChatDigestRequest) -> dict[str, str]:
        if request.chat_id is not None:
            result = await self.lark.execute(
                [
                    "im",
                    "chats",
                    "get",
                    "--chat-id",
                    request.chat_id,
                    "--as",
                    request.identity.value,
                    "--format",
                    "json",
                ]
            )
            name = find_first_value(result.data, {"name"})
            if not isinstance(name, str) or not name:
                raise NotFoundError(
                    "the supplied chat_id did not resolve to a named visible group",
                    details={"chat_id": request.chat_id},
                )
            return {"chat_id": request.chat_id, "name": name}

        assert request.chat_name is not None
        result = await self.lark.execute(
            [
                "im",
                "+chat-search",
                "--query",
                request.chat_name[:64],
                "--disable-search-by-user",
                "--page-all",
                "--page-limit",
                "3",
                "--as",
                request.identity.value,
                "--format",
                "json",
            ]
        )
        chats = result.data.get("chats", [])
        pagination = result.meta.get("pagination")
        incomplete = isinstance(pagination, dict) and pagination.get("complete") is False
        if result.data.get("has_more") is True or incomplete:
            raise _validation("group search pagination was incomplete; refine the exact group name")
        expected = _normalized(request.chat_name)
        candidates = (
            [
                {"chat_id": chat.get("chat_id"), "name": chat.get("name")}
                for chat in chats
                if isinstance(chat, dict)
                and isinstance(chat.get("chat_id"), str)
                and isinstance(chat.get("name"), str)
                and _normalized(chat["name"]) == expected
            ]
            if isinstance(chats, list)
            else []
        )
        if not candidates:
            raise NotFoundError(
                "no visible Feishu group matched the exact name",
                details={"chat_name": request.chat_name},
            )
        if len(candidates) > 1:
            raise AmbiguityError(
                "more than one visible Feishu group matched the exact name",
                details={"chat_name": request.chat_name, "candidates": candidates},
            )
        candidate = candidates[0]
        chat_id = candidate["chat_id"]
        name = candidate["name"]
        assert isinstance(chat_id, str)
        assert isinstance(name, str)
        return {"chat_id": chat_id, "name": name}

    @staticmethod
    def _downloaded_path(data: dict[str, Any], workspace: Path, index: int) -> Path:
        saved = find_first_value(data, {"saved_path"})
        if isinstance(saved, str) and saved:
            candidate = Path(saved)
            if not candidate.is_absolute():
                candidate = workspace / candidate
            candidate = candidate.resolve()
            if candidate.parent == workspace and candidate.is_file():
                return candidate
        matches = [path for path in workspace.glob(f"chat-image-{index}*") if path.is_file()]
        if len(matches) == 1 and matches[0].resolve().parent == workspace:
            return matches[0].resolve()
        raise _validation("downloaded image path was not safely resolvable")
