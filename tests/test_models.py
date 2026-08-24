from __future__ import annotations

import pytest
from pydantic import ValidationError

from codex2lark.core.models import (
    ChatDigestRequest,
    DiagramFormat,
    EditCommand,
    EditDocumentRequest,
    EditOperation,
    ResourceRef,
    RichTextSpan,
    TableBlock,
    TableCell,
    WhiteboardRenderRequest,
)


def test_resource_requires_exactly_one_locator() -> None:
    with pytest.raises(ValidationError):
        ResourceRef()
    with pytest.raises(ValidationError):
        ResourceRef(url="https://example", token="token")
    assert ResourceRef(token="docx_test").value == "docx_test"


def test_models_reject_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ResourceRef.model_validate({"token": "x", "unexpected": True})


def test_edit_operation_requires_command_specific_fields() -> None:
    with pytest.raises(ValidationError):
        EditOperation(command=EditCommand.STR_REPLACE, content="new")
    with pytest.raises(ValidationError):
        EditOperation(command=EditCommand.BLOCK_DELETE)
    assert EditOperation(command=EditCommand.STR_REPLACE, pattern="old", content="new")


def test_edit_request_requires_one_target_and_change_summary() -> None:
    operation = EditOperation(command=EditCommand.APPEND, content="<p>new</p>")
    with pytest.raises(ValidationError):
        EditDocumentRequest(operations=[operation], change_summary="新增内容")
    with pytest.raises(ValidationError):
        EditDocumentRequest(
            resource=ResourceRef(token="docx_test"),
            document_title="Test",
            operations=[operation],
            change_summary="新增内容",
        )
    assert EditDocumentRequest(
        document_title="Test",
        operations=[operation],
        change_summary="新增内容",
    )


def test_chat_digest_requires_one_group_and_explicit_range() -> None:
    with pytest.raises(ValidationError):
        ChatDigestRequest(start="2026-08-24", end="2026-08-25")
    with pytest.raises(ValidationError):
        ChatDigestRequest(
            chat_id="oc_test",
            chat_name="项目群",
            start="2026-08-24",
            end="2026-08-25",
        )
    with pytest.raises(ValidationError):
        ChatDigestRequest(chat_name="项目群", start="2026-08-24", end="2026-08-24")
    assert ChatDigestRequest(
        chat_name="项目群",
        start="2026-08-24",
        end="2026-08-25",
    )


def test_whiteboard_target_depends_on_mode() -> None:
    with pytest.raises(ValidationError):
        WhiteboardRenderRequest(mode="create", format=DiagramFormat.MERMAID, source="graph TD")
    with pytest.raises(ValidationError):
        WhiteboardRenderRequest(mode="update", format=DiagramFormat.SVG, source="<svg/>")


def test_rich_text_and_table_shape_are_strict() -> None:
    with pytest.raises(ValidationError):
        RichTextSpan(text="unsafe", link="http://example.com")
    with pytest.raises(ValidationError):
        RichTextSpan(text="x + y", formula=True, bold=True)
    with pytest.raises(ValidationError):
        TableBlock(
            type="table",
            headers=[TableCell(content=[RichTextSpan(text="A")])],
            rows=[
                [
                    TableCell(content=[RichTextSpan(text="one")]),
                    TableCell(content=[RichTextSpan(text="two")]),
                ]
            ],
        )
