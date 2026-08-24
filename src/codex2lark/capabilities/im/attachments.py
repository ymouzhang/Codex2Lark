from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import PurePath
from typing import Protocol

from defusedxml import ElementTree  # type: ignore[import-untyped]
from pypdf import PdfReader

from codex2lark.runtime.context import ContextEvidence
from codex2lark.storage.blobs import EncryptedBlobStore
from codex2lark.storage.capacity import StorageCapacityMonitor

from .models import StoredAttachment


@dataclass(frozen=True, slots=True)
class AttachmentLoadRequest:
    tenant_key: str
    app_id: str
    chat_id: str
    message_id: str
    resource_key: str


@dataclass(frozen=True, slots=True)
class ParseResult:
    state: str
    content_kind: str
    content: str | None
    warning_code: str | None = None
    truncated: bool = False
    parser_id: str = "codex2lark.safe_attachment"
    parser_version: str = "1"


@dataclass(frozen=True, slots=True)
class AttachmentEvidence:
    blob_id: str
    content_kind: str
    evidence: ContextEvidence
    warning_code: str | None


class AttachmentRepository(Protocol):
    async def get_attachment(
        self,
        tenant_key: str,
        app_id: str,
        chat_id: str,
        message_id: str,
        resource_key: str,
    ) -> StoredAttachment | None: ...

    async def record_attachment_blob(
        self,
        attachment: StoredAttachment,
        *,
        blob_id: str,
        byte_size: int,
        media_type: str | None,
        now_ms: int,
    ) -> None: ...

    async def record_attachment_parse(
        self,
        attachment: StoredAttachment,
        *,
        parser_id: str,
        parser_version: str,
        parsing_policy_version: str,
        content: str | None,
        state: str,
        warning_code: str | None,
    ) -> None: ...


class AttachmentDownloader(Protocol):
    async def download_resource(
        self, resource_key: str, resource_type: str, *, message_id: str
    ) -> bytes | None: ...


class SafeAttachmentParser:
    def __init__(
        self,
        *,
        max_output_chars: int = 200_000,
        max_zip_entries: int = 1_000,
        max_zip_uncompressed_bytes: int = 50 * 1024 * 1024,
        max_zip_ratio: int = 100,
    ) -> None:
        if (
            min(
                max_output_chars,
                max_zip_entries,
                max_zip_uncompressed_bytes,
                max_zip_ratio,
            )
            < 1
        ):
            raise ValueError("attachment parser limits must be positive")
        self._max_output_chars = max_output_chars
        self._max_zip_entries = max_zip_entries
        self._max_zip_uncompressed_bytes = max_zip_uncompressed_bytes
        self._max_zip_ratio = max_zip_ratio

    def parse(self, attachment: StoredAttachment, content: bytes) -> ParseResult:
        suffix = PurePath(attachment.filename or "").suffix.lower()
        if self._is_active_or_archive(suffix, content):
            return ParseResult("blocked", "blocked", None, "active_content_blocked")
        if attachment.resource_type in {"audio", "video"}:
            return ParseResult(
                "metadata_only",
                "media_metadata",
                self._metadata(attachment, len(content)),
                "media_not_transcribed",
            )
        if attachment.resource_type == "image" or suffix in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".webp",
        }:
            return ParseResult("metadata_only", "image", self._metadata(attachment, len(content)))
        try:
            if suffix in {".txt", ".md", ".markdown"}:
                return self._text(content)
            if suffix == ".json":
                decoded = content.decode("utf-8-sig")
                parsed = json.loads(decoded)
                return self._bounded(json.dumps(parsed, ensure_ascii=False, indent=2), "json")
            if suffix == ".csv":
                return self._csv(content)
            if suffix == ".pdf":
                return self._pdf(content)
            if suffix == ".docx":
                return self._docx(content)
            if suffix == ".xlsx":
                return self._xlsx(content)
            if suffix == ".pptx":
                return self._pptx(content)
        except Exception:
            return ParseResult("failed", "unknown", None, "parse_failed")
        return ParseResult(
            "metadata_only",
            "file_metadata",
            self._metadata(attachment, len(content)),
            "unsupported_file_type",
        )

    def _text(self, content: bytes) -> ParseResult:
        return self._bounded(content.decode("utf-8-sig"), "text")

    def _csv(self, content: bytes) -> ParseResult:
        reader = csv.reader(io.StringIO(content.decode("utf-8-sig")))
        lines: list[str] = []
        for index, row in enumerate(reader):
            if index >= 10_000:
                break
            lines.append(" | ".join(row))
        return self._bounded("\n".join(lines), "table")

    def _pdf(self, content: bytes) -> ParseResult:
        reader = PdfReader(io.BytesIO(content), strict=True)
        if reader.is_encrypted:
            return ParseResult("blocked", "pdf", None, "encrypted_pdf")
        text = "\n\n".join((page.extract_text() or "") for page in reader.pages[:100])
        return self._bounded(text, "pdf")

    def _docx(self, content: bytes) -> ParseResult:
        with self._safe_zip(content) as archive:
            root = ElementTree.fromstring(archive.read("word/document.xml"))
            paragraphs = []
            for paragraph in root.findall(".//{*}p"):
                text = "".join(node.text or "" for node in paragraph.findall(".//{*}t"))
                if text:
                    paragraphs.append(text)
            return self._bounded("\n".join(paragraphs), "document")

    def _xlsx(self, content: bytes) -> ParseResult:
        with self._safe_zip(content) as archive:
            names = set(archive.namelist())
            shared: list[str] = []
            if "xl/sharedStrings.xml" in names:
                root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
                shared = [
                    "".join(node.text or "" for node in item.findall(".//{*}t"))
                    for item in root.findall(".//{*}si")
                ]
            output: list[str] = []
            sheets = sorted(
                name
                for name in names
                if name.startswith("xl/worksheets/sheet") and name.endswith(".xml")
            )
            for name in sheets[:100]:
                output.append(f"[{name}]")
                root = ElementTree.fromstring(archive.read(name))
                for cell in root.findall(".//{*}c")[:100_000]:
                    ref = cell.attrib.get("r", "?")
                    formula = cell.findtext("{*}f")
                    value = cell.findtext("{*}v") or ""
                    if cell.attrib.get("t") == "s" and value.isdigit():
                        index = int(value)
                        value = shared[index] if index < len(shared) else value
                    rendered = f"{ref}={value}"
                    if formula:
                        rendered += f" [formula:{formula}]"
                    output.append(rendered)
            return self._bounded("\n".join(output), "workbook")

    def _pptx(self, content: bytes) -> ParseResult:
        with self._safe_zip(content) as archive:
            names = sorted(
                name
                for name in archive.namelist()
                if name.startswith("ppt/slides/slide") and name.endswith(".xml")
            )
            output: list[str] = []
            for index, name in enumerate(names[:500], start=1):
                root = ElementTree.fromstring(archive.read(name))
                text = " ".join(node.text or "" for node in root.findall(".//{*}t"))
                output.append(f"[slide {index}] {text}")
            return self._bounded("\n".join(output), "presentation")

    def _safe_zip(self, content: bytes) -> zipfile.ZipFile:
        archive = zipfile.ZipFile(io.BytesIO(content))
        entries = archive.infolist()
        if len(entries) > self._max_zip_entries:
            archive.close()
            raise ValueError("ZIP entry limit exceeded")
        total = 0
        for entry in entries:
            total += entry.file_size
            if total > self._max_zip_uncompressed_bytes:
                archive.close()
                raise ValueError("ZIP uncompressed size limit exceeded")
            denominator = max(1, entry.compress_size)
            if entry.file_size / denominator > self._max_zip_ratio:
                archive.close()
                raise ValueError("ZIP compression ratio limit exceeded")
        return archive

    def _bounded(self, value: str, content_kind: str) -> ParseResult:
        if len(value) <= self._max_output_chars:
            return ParseResult("parsed", content_kind, value)
        return ParseResult(
            "parsed",
            content_kind,
            value[: self._max_output_chars],
            "parser_output_truncated",
            True,
        )

    @staticmethod
    def _metadata(attachment: StoredAttachment, byte_size: int) -> str:
        return (
            f"filename={attachment.filename or 'unknown'}\n"
            f"resource_type={attachment.resource_type}\n"
            f"media_type={attachment.media_type or 'unknown'}\n"
            f"byte_size={byte_size}"
        )

    @staticmethod
    def _is_active_or_archive(suffix: str, content: bytes) -> bool:
        if suffix in {
            ".zip",
            ".rar",
            ".7z",
            ".tar",
            ".gz",
            ".exe",
            ".dll",
            ".sh",
            ".bat",
            ".cmd",
            ".ps1",
            ".py",
            ".js",
            ".jar",
        }:
            return True
        return content.startswith((b"MZ", b"\x7fELF"))


class AttachmentService:
    def __init__(
        self,
        repository: AttachmentRepository,
        downloader: AttachmentDownloader,
        blobs: EncryptedBlobStore,
        parser: SafeAttachmentParser,
        *,
        max_attachment_bytes: int = 20 * 1024 * 1024,
        parsing_policy_version: str = "1",
        capacity: StorageCapacityMonitor | None = None,
    ) -> None:
        if max_attachment_bytes < 1 or not parsing_policy_version:
            raise ValueError("attachment byte limit and parsing policy version are required")
        self._repository = repository
        self._downloader = downloader
        self._blobs = blobs
        self._parser = parser
        self._max_attachment_bytes = max_attachment_bytes
        self._parsing_policy_version = parsing_policy_version
        self._capacity = capacity

    async def load(self, request: AttachmentLoadRequest, *, now_ms: int) -> AttachmentEvidence:
        attachment = await self._repository.get_attachment(
            request.tenant_key,
            request.app_id,
            request.chat_id,
            request.message_id,
            request.resource_key,
        )
        if attachment is None:
            raise PermissionError("attachment is not referenced by the trusted message binding")
        if (
            attachment.declared_size is not None
            and attachment.declared_size > self._max_attachment_bytes
        ):
            raise ValueError("attachment declared size exceeds policy")
        if attachment.blob_id is None:
            requested = attachment.declared_size or self._max_attachment_bytes
            if (
                self._capacity is not None
                and not self._capacity.snapshot(requested_bytes=requested).permits_download
            ):
                return self._storage_pressure_evidence(attachment)
            content = await self._downloader.download_resource(
                attachment.resource_key,
                attachment.resource_type,
                message_id=attachment.message_id,
            )
            if content is None:
                raise RuntimeError("Feishu attachment download returned no bytes")
            if len(content) > self._max_attachment_bytes:
                raise ValueError("attachment actual size exceeds policy")
            if (
                self._capacity is not None
                and not self._capacity.snapshot(requested_bytes=len(content)).permits_download
            ):
                return self._storage_pressure_evidence(attachment)
            blob_id = self._blobs.put(content)
            await self._repository.record_attachment_blob(
                attachment,
                blob_id=blob_id,
                byte_size=len(content),
                media_type=attachment.media_type,
                now_ms=now_ms,
            )
        else:
            blob_id = attachment.blob_id
            content = self._blobs.get(blob_id)
        result = self._parser.parse(attachment, content)
        await self._repository.record_attachment_parse(
            attachment,
            parser_id=result.parser_id,
            parser_version=result.parser_version,
            parsing_policy_version=self._parsing_policy_version,
            content=result.content,
            state=result.state,
            warning_code=result.warning_code,
        )
        evidence_content = result.content or (
            f"Attachment {attachment.filename or attachment.resource_key} "
            f"was not parsed: {result.warning_code or result.state}."
        )
        return AttachmentEvidence(
            blob_id=blob_id,
            content_kind=result.content_kind,
            evidence=ContextEvidence(
                source_ref=(f"im.attachment:{attachment.message_id}:{attachment.resource_key}"),
                content=evidence_content,
                source_version=blob_id,
            ),
            warning_code=result.warning_code,
        )

    @staticmethod
    def _storage_pressure_evidence(attachment: StoredAttachment) -> AttachmentEvidence:
        return AttachmentEvidence(
            blob_id="not-downloaded",
            content_kind="file_metadata",
            evidence=ContextEvidence(
                source_ref=(f"im.attachment:{attachment.message_id}:{attachment.resource_key}"),
                content=(
                    f"Attachment {attachment.filename or attachment.resource_key} was not "
                    "downloaded because local storage is at its hard capacity threshold."
                ),
                source_version="storage-pressure-hard",
            ),
            warning_code="storage_pressure_hard",
        )
