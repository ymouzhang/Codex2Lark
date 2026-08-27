from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from codex2lark.adapters.lark_cli import LarkCli, LarkCliResult
from codex2lark.core.errors import AmbiguityError, ConflictError
from codex2lark.core.models import (
    DocumentSpec,
    EditCommand,
    EditDocumentRequest,
    EditOperation,
    ParagraphBlock,
    PublishDocumentRequest,
    ResourceRef,
    RichTextSpan,
    VerificationPolicy,
)
from codex2lark.services.docs import DocsService


class StubDrive:
    def __init__(self, *, existing: bool = False) -> None:
        self.resolved_titles: list[str] = []
        self.existing = existing

    async def ensure_managed_folder(self, identity: object) -> dict[str, object]:
        return {
            "title": "Codex2Lark",
            "token": "fld_managed",
            "url": "https://example.feishu.cn/drive/folder/fld_managed",
        }

    async def search_documents(self, title: str, identity: object) -> dict[str, object]:
        return {"ok": True, "query": title, "scope": "drive", "matches": []}

    async def search_managed_documents(
        self, title: str, identity: object, folder: dict[str, object]
    ) -> dict[str, object]:
        del identity, folder
        return {
            "ok": True,
            "query": title,
            "scope": "managed_folder",
            "matches": ([{"title": title, "token": "docx_existing"}] if self.existing else []),
        }

    async def resolve_document(self, title: str, identity: object) -> dict[str, object]:
        self.resolved_titles.append(title)
        return {
            "title": title,
            "token": "docx_test",
            "url": "https://example.feishu.cn/docx/docx_test",
        }


class StubNotifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.summaries: list[str] = []
        self.titles: list[str] = []

    async def document_edited(self, **kwargs: object) -> dict[str, object]:
        summary = kwargs["change_summary"]
        title = kwargs["document_title"]
        assert isinstance(summary, str)
        assert isinstance(title, str)
        self.summaries.append(summary)
        self.titles.append(title)
        if self.fail:
            raise RuntimeError("notification unavailable")
        return {"status": "sent", "message_id": "om_test"}


class RecordingLarkCli(LarkCli):
    def __init__(self, content: str | None = None) -> None:
        super().__init__("unused")
        self.calls: list[tuple[tuple[str, ...], Path | None]] = []
        self.uploaded_content: str | None = None
        self.content = content or (
            '<title>Test</title><h1 id="overview">Overview</h1><p id="expected">Expected text</p>'
        )

    async def execute(self, args: Sequence[str], *, cwd: Path | None = None) -> LarkCliResult:
        self.calls.append((tuple(args), cwd))
        if "--content" in args:
            reference = args[args.index("--content") + 1]
            assert reference.startswith("@./")
            assert cwd is not None
            self.uploaded_content = (cwd / reference[3:]).read_text(encoding="utf-8")
        return LarkCliResult(
            data={
                "document": {
                    "document_id": "docx_test",
                    "revision_id": 7,
                    "title": "Test",
                    "url": "https://example.feishu.cn/docx/docx_test",
                    "content": self.content,
                }
            },
            identity="user",
        )


def service() -> tuple[DocsService, RecordingLarkCli]:
    lark = RecordingLarkCli()
    return DocsService(lark, StubDrive(), StubNotifier()), lark


@pytest.mark.asyncio
async def test_publish_compiles_creates_and_reads_back() -> None:
    docs, lark = service()
    result = await docs.publish(
        PublishDocumentRequest(
            document=DocumentSpec(
                title="Test",
                blocks=[
                    ParagraphBlock(
                        type="paragraph",
                        content=[RichTextSpan(text="Expected text")],
                    )
                ],
            ),
            verification=VerificationPolicy(required_text=["Expected text"]),
        )
    )
    assert result["ok"] is True
    assert result["verification"]["status"] == "passed"
    assert lark.uploaded_content is not None
    assert '<p align="left">Expected text</p>' in lark.uploaded_content
    assert lark.calls[0][0][:2] == ("docs", "+create")
    assert lark.calls[0][0][-2:] == ("--parent-token", "fld_managed")
    assert lark.calls[1][0][:2] == ("docs", "+fetch")


@pytest.mark.asyncio
async def test_create_refuses_existing_exact_title_before_upload() -> None:
    lark = RecordingLarkCli()
    docs = DocsService(lark, StubDrive(existing=True), StubNotifier())

    with pytest.raises(ConflictError, match="exact title"):
        await docs.publish(
            PublishDocumentRequest(
                document=DocumentSpec(
                    title="Test",
                    blocks=[
                        ParagraphBlock(
                            type="paragraph",
                            content=[RichTextSpan(text="Expected text")],
                        )
                    ],
                )
            )
        )

    assert lark.calls == []


@pytest.mark.asyncio
async def test_edit_rejects_stale_revision_before_write() -> None:
    docs, lark = service()
    with pytest.raises(ConflictError):
        await docs.edit(
            EditDocumentRequest(
                resource=ResourceRef(token="docx_test"),
                expected_revision=6,
                operations=[EditOperation(command=EditCommand.APPEND, content="<p>new</p>")],
                change_summary="新增说明",
            )
        )
    assert len(lark.calls) == 1
    assert lark.calls[0][0][:2] == ("docs", "+fetch")


@pytest.mark.asyncio
async def test_edit_rejects_ambiguous_exact_replacement() -> None:
    lark = RecordingLarkCli("<title>Test</title><p>same</p><p>same</p>")
    docs = DocsService(lark)

    with pytest.raises(AmbiguityError):
        await docs.edit(
            EditDocumentRequest(
                resource=ResourceRef(token="docx_test"),
                operations=[
                    EditOperation(
                        command=EditCommand.STR_REPLACE,
                        pattern="same",
                        content="different",
                    )
                ],
                change_summary="替换重复文本",
            )
        )
    assert len(lark.calls) == 1


@pytest.mark.asyncio
async def test_edit_refetches_before_following_block_operation() -> None:
    docs, lark = service()

    result = await docs.edit(
        EditDocumentRequest(
            resource=ResourceRef(token="docx_test"),
            operations=[
                EditOperation(
                    command=EditCommand.BLOCK_REPLACE,
                    block_id="overview",
                    content="<h1>New overview</h1>",
                ),
                EditOperation(
                    command=EditCommand.BLOCK_DELETE,
                    block_id="expected",
                ),
            ],
            change_summary="更新概览并删除旧段落",
        )
    )

    assert result["operations_applied"] == 2
    assert [call[0][1] for call in lark.calls] == [
        "+fetch",
        "+update",
        "+fetch",
        "+update",
        "+fetch",
    ]
    assert result["notification"]["status"] == "sent"


@pytest.mark.asyncio
async def test_edit_resolves_exact_document_title_before_inspection() -> None:
    lark = RecordingLarkCli()
    drive = StubDrive()
    notifier = StubNotifier()
    docs = DocsService(lark, drive, notifier)

    result = await docs.edit(
        EditDocumentRequest(
            document_title="Test",
            operations=[EditOperation(command=EditCommand.APPEND, content="<p>new</p>")],
            change_summary="新增一段说明",
        )
    )

    assert drive.resolved_titles == ["Test"]
    assert lark.calls[0][0][lark.calls[0][0].index("--doc") + 1].endswith("docx_test")
    assert result["notification"]["status"] == "sent"
    assert notifier.summaries == ["新增一段说明"]


@pytest.mark.asyncio
async def test_verified_edit_reports_notification_failure_without_failing_edit() -> None:
    lark = RecordingLarkCli()
    docs = DocsService(lark, StubDrive(), StubNotifier(fail=True))

    result = await docs.edit(
        EditDocumentRequest(
            resource=ResourceRef(token="docx_test"),
            operations=[EditOperation(command=EditCommand.APPEND, content="<p>new</p>")],
            change_summary="新增一段说明",
        )
    )

    assert result["ok"] is True
    assert result["verification"]["status"] == "passed"
    assert result["notification"]["status"] == "failed"
    assert "do not repeat the edit" in result["warnings"][-1]
