from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from codex2lark.docs_service import DocsService
from codex2lark.errors import AmbiguityError, ConflictError
from codex2lark.lark_cli import LarkCli, LarkCliResult
from codex2lark.models import (
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
    return DocsService(lark), lark


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
    assert lark.calls[1][0][:2] == ("docs", "+fetch")


@pytest.mark.asyncio
async def test_edit_rejects_stale_revision_before_write() -> None:
    docs, lark = service()
    with pytest.raises(ConflictError):
        await docs.edit(
            EditDocumentRequest(
                resource=ResourceRef(token="docx_test"),
                expected_revision=6,
                operations=[EditOperation(command=EditCommand.APPEND, content="<p>new</p>")],
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
