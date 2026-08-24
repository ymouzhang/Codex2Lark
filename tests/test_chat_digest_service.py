from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from codex2lark.chat_digest_service import ChatDigestService, _normalize_message
from codex2lark.errors import AmbiguityError, Codex2LarkError
from codex2lark.lark_cli import LarkCliResult
from codex2lark.models import ChatDigestRequest


class DigestDrive:
    def __init__(self, *, existing: bool = False) -> None:
        self.calls = 0
        self.existing = existing

    async def search_documents(self, title: str, identity: object) -> dict[str, object]:
        folder = {"title": "Codex2Lark", "token": "fld_managed"}
        if self.existing:
            return {
                "ok": True,
                "scope": "managed_folder",
                "managed_folder": folder,
                "matches": [
                    {
                        "title": title,
                        "token": "docx_digest",
                        "url": "https://example.feishu.cn/docx/docx_digest",
                    }
                ],
            }
        return {
            "ok": True,
            "scope": "drive",
            "managed_folder": None,
            "matches": [],
        }

    async def ensure_managed_folder(self, identity: object) -> dict[str, object]:
        self.calls += 1
        return {"title": "Codex2Lark", "token": "fld_managed"}


class DigestDocs:
    def __init__(self, *, recognized: bool = True, revisions: list[int] | None = None) -> None:
        self.recognized = recognized
        self.revisions = revisions or [1]
        self.calls = 0

    async def inspect(self, request: object) -> dict[str, Any]:
        revision = self.revisions[min(self.calls, len(self.revisions) - 1)]
        self.calls += 1
        marker = "<h1>群聊记录</h1>" if self.recognized else "<h1>普通文档</h1>"
        content = f"<title>项目群</title>{marker}<p>文件: plan.pdf (未下载)</p>"
        data = {
            "document": {
                "document_id": "docx_digest",
                "revision_id": revision,
                "title": "项目群",
                "url": "https://example.feishu.cn/docx/docx_digest",
                "content": content,
            }
        }
        return {
            "ok": True,
            "resource": data["document"],
            "data": data,
            "revision": revision,
            "warnings": [],
        }


class DigestLark:
    def __init__(
        self,
        *,
        messages: list[dict[str, Any]],
        chats: list[dict[str, Any]] | None = None,
        incomplete: bool = False,
        image_failure: bool = False,
    ) -> None:
        self.messages = messages
        self.chats = chats or [{"chat_id": "oc_project", "name": "项目群"}]
        self.incomplete = incomplete
        self.image_failure = image_failure
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []
        self.uploaded_xml = ""
        self.workspace: Path | None = None

    async def execute(self, args: Sequence[str], *, cwd: Path | None = None) -> LarkCliResult:
        call = tuple(args)
        self.calls.append((call, cwd))
        if call[:2] == ("im", "+chat-search"):
            return LarkCliResult(data={"chats": self.chats})
        if call[:3] == ("im", "chats", "get"):
            return LarkCliResult(data={"chat": {"chat_id": "oc_project", "name": "项目群"}})
        if call[:2] == ("im", "+chat-messages-list"):
            return LarkCliResult(
                data={"messages": self.messages, "has_more": self.incomplete},
                meta={"pagination": {"complete": not self.incomplete}},
            )
        if call[:2] == ("im", "+messages-resources-download"):
            if self.image_failure:
                raise RuntimeError("image unavailable")
            assert cwd is not None
            output = call[call.index("--output") + 1]
            target = cwd / f"{Path(output).name}.png"
            target.write_bytes(b"fake-png")
            return LarkCliResult(data={"saved_path": target.name, "size_bytes": 8})
        if call[:2] == ("docs", "+create"):
            assert cwd is not None
            self.workspace = cwd
            reference = call[call.index("--content") + 1]
            self.uploaded_xml = (cwd / reference.removeprefix("@./")).read_text()
            return LarkCliResult(
                data={
                    "document": {
                        "document_id": "docx_digest",
                        "revision_id": 1,
                        "title": "项目群",
                        "url": "https://example.feishu.cn/docx/docx_digest",
                    }
                }
            )
        if call[:2] == ("docs", "+update"):
            assert cwd is not None
            self.workspace = cwd
            reference = call[call.index("--content") + 1]
            self.uploaded_xml = (cwd / reference.removeprefix("@./")).read_text()
            return LarkCliResult(data={"document_id": "docx_digest", "revision_id": 2})
        raise AssertionError(f"unexpected call: {call}")


class DigestNotifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def document_edited(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"status": "sent", "message_id": "om_notice"}


def sample_messages() -> list[dict[str, Any]]:
    return [
        {
            "message_id": "om_text",
            "msg_type": "text",
            "create_time": "1787572802000",
            "sender": {"name": "李四"},
            "content": json.dumps({"text": "讨论 <部署> 方案"}),
            "thread_replies": [
                {
                    "message_id": "om_reply",
                    "msg_type": "text",
                    "create_time": "1787572802500",
                    "sender": {"name": "王五"},
                    "content": json.dumps({"text": "同意"}),
                }
            ],
        },
        {
            "message_id": "om_file",
            "msg_type": "file",
            "create_time": "1787572801000",
            "sender": {"name": "张三"},
            "content": json.dumps({"file_key": "file_secret", "file_name": "plan.pdf"}),
        },
        {
            "message_id": "om_image",
            "msg_type": "image",
            "create_time": "1787572803000",
            "sender": {"name": "赵六"},
            "content": json.dumps({"image_key": "img_test"}),
        },
    ]


def test_post_resource_markers_become_image_and_filename_metadata() -> None:
    entry = _normalize_message(
        {
            "message_id": "om_post",
            "msg_type": "post",
            "create_time": "1787572800000",
            "sender": {"name": "张三"},
            "content": (
                '发布说明 ![架构图](img_post) <file key="file_post" name="architecture.pdf"/>'
            ),
        }
    )

    assert entry.image_keys == ("img_post",)
    assert entry.file_names == ("architecture.pdf",)
    assert "![架构图]" not in entry.text
    assert "文件: architecture.pdf (未下载)" in entry.text


@pytest.mark.asyncio
async def test_digest_orders_messages_inserts_images_and_never_downloads_files() -> None:
    lark = DigestLark(messages=sample_messages())
    drive = DigestDrive()
    service = ChatDigestService(lark, DigestDocs(), drive)  # type: ignore[arg-type]

    result = await service.publish(
        ChatDigestRequest(
            chat_name="项目群",
            start="2026-08-24",
            end="2026-08-25",
        )
    )

    assert result["ok"] is True
    assert result["action"] == "created"
    assert result["notification"]["status"] == "not_applicable"
    assert result["stats"] == {
        "messages": 4,
        "images_found": 1,
        "images_inserted": 1,
        "files_listed": 1,
    }
    assert lark.uploaded_xml.index("张三") < lark.uploaded_xml.index("李四")
    assert lark.uploaded_xml.index("李四") < lark.uploaded_xml.index("王五")
    assert lark.uploaded_xml.index("王五") < lark.uploaded_xml.index("赵六")
    assert "讨论 &lt;部署&gt; 方案" in lark.uploaded_xml
    assert "文件: plan.pdf (未下载)" in lark.uploaded_xml
    assert '<img path="@./chat-image-0.png"' in lark.uploaded_xml
    downloads = [
        call for call, _ in lark.calls if call[:2] == ("im", "+messages-resources-download")
    ]
    assert len(downloads) == 1
    assert downloads[0][downloads[0].index("--type") + 1] == "image"
    assert "file_secret" not in downloads[0]
    create = next(call for call, _ in lark.calls if call[:2] == ("docs", "+create"))
    assert create[create.index("--parent-token") + 1] == "fld_managed"
    assert lark.workspace is not None
    assert not lark.workspace.exists()


@pytest.mark.asyncio
async def test_incomplete_history_stops_before_any_write() -> None:
    lark = DigestLark(messages=sample_messages(), incomplete=True)
    drive = DigestDrive()
    service = ChatDigestService(lark, DigestDocs(), drive)  # type: ignore[arg-type]

    with pytest.raises(Codex2LarkError, match="pagination was incomplete"):
        await service.publish(
            ChatDigestRequest(
                chat_name="项目群",
                start="2026-08-24",
                end="2026-08-25",
            )
        )

    assert drive.calls == 0
    assert all(call[:2] != ("docs", "+create") for call, _ in lark.calls)


@pytest.mark.asyncio
async def test_duplicate_exact_group_names_are_ambiguous() -> None:
    lark = DigestLark(
        messages=[],
        chats=[
            {"chat_id": "oc_one", "name": "项目群"},
            {"chat_id": "oc_two", "name": "项目群"},
        ],
    )
    service = ChatDigestService(lark, DigestDocs(), DigestDrive())  # type: ignore[arg-type]

    with pytest.raises(AmbiguityError):
        await service.publish(
            ChatDigestRequest(
                chat_name="项目群",
                start="2026-08-24",
                end="2026-08-25",
            )
        )

    assert len(lark.calls) == 1


@pytest.mark.asyncio
async def test_image_failure_is_visible_but_digest_is_still_verified() -> None:
    lark = DigestLark(messages=sample_messages(), image_failure=True)
    service = ChatDigestService(lark, DigestDocs(), DigestDrive())  # type: ignore[arg-type]

    result = await service.publish(
        ChatDigestRequest(
            chat_id="oc_project",
            start="2026-08-24",
            end="2026-08-25",
        )
    )

    assert result["stats"]["images_inserted"] == 0
    assert "图片无法读取: img_test" in lark.uploaded_xml
    assert any("could not be inserted" in warning for warning in result["warnings"])


@pytest.mark.asyncio
async def test_existing_recognized_digest_is_refreshed_and_notified() -> None:
    lark = DigestLark(messages=sample_messages())
    drive = DigestDrive(existing=True)
    notifier = DigestNotifier()
    service = ChatDigestService(  # type: ignore[arg-type]
        lark,
        DigestDocs(),
        drive,
        notifier,
    )

    result = await service.publish(
        ChatDigestRequest(
            chat_name="项目群",
            start="2026-08-24",
            end="2026-08-25",
        )
    )

    assert result["action"] == "updated"
    assert result["notification"]["status"] == "sent"
    assert drive.calls == 0
    assert any(call[:2] == ("docs", "+update") for call, _ in lark.calls)
    assert all(call[:2] != ("docs", "+create") for call, _ in lark.calls)
    assert notifier.calls[0]["operations_applied"] == 1


@pytest.mark.asyncio
async def test_unrecognized_same_title_document_is_never_overwritten() -> None:
    lark = DigestLark(messages=sample_messages())
    service = ChatDigestService(  # type: ignore[arg-type]
        lark,
        DigestDocs(recognized=False),
        DigestDrive(existing=True),
        DigestNotifier(),
    )

    with pytest.raises(Codex2LarkError, match="not a recognized group digest"):
        await service.publish(
            ChatDigestRequest(
                chat_name="项目群",
                start="2026-08-24",
                end="2026-08-25",
            )
        )

    assert all(call[:2] != ("docs", "+update") for call, _ in lark.calls)


@pytest.mark.asyncio
async def test_digest_refresh_stops_when_live_revision_changes() -> None:
    lark = DigestLark(messages=sample_messages())
    service = ChatDigestService(  # type: ignore[arg-type]
        lark,
        DigestDocs(revisions=[1, 2]),
        DigestDrive(existing=True),
        DigestNotifier(),
    )

    with pytest.raises(Codex2LarkError, match="changed before refresh"):
        await service.publish(
            ChatDigestRequest(
                chat_name="项目群",
                start="2026-08-24",
                end="2026-08-25",
            )
        )

    assert all(call[:2] != ("docs", "+update") for call, _ in lark.calls)
