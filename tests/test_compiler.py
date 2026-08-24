from __future__ import annotations

import pytest

from codex2lark.authoring.compiler import (
    block_exists,
    compile_document,
    count_exact_pattern,
    preflight_content,
    whiteboard_xml,
)
from codex2lark.core.errors import Codex2LarkError
from codex2lark.core.models import (
    CalloutBlock,
    CodeBlock,
    DiagramFormat,
    DocumentFormat,
    DocumentSpec,
    HeadingBlock,
    RichTextSpan,
    TableBlock,
    TableCell,
    WhiteboardBlock,
)


def test_xml_preflight_accepts_supported_structure() -> None:
    preflight_content(
        "<title>Plan</title><h1>Overview</h1><table><tr><td>A</td></tr></table>",
        DocumentFormat.XML,
    )


def test_xml_preflight_rejects_invalid_and_unsupported_structure() -> None:
    with pytest.raises(Codex2LarkError):
        preflight_content("<p>", DocumentFormat.XML)
    with pytest.raises(Codex2LarkError):
        preflight_content("<script>alert(1)</script>", DocumentFormat.XML)
    with pytest.raises(Codex2LarkError):
        preflight_content("<!DOCTYPE x><p>no</p>", DocumentFormat.XML)


def test_whiteboard_source_is_xml_escaped() -> None:
    value = whiteboard_xml(DiagramFormat.MERMAID, "A --> B & C")
    assert value == '<whiteboard type="mermaid">A --&gt; B &amp; C</whiteboard>'


def test_live_xml_selectors_validate_blocks_and_exact_counts() -> None:
    content = '<h1 id="block-1">Overview</h1><p>same</p><p>same</p>'
    assert block_exists(content, "block-1")
    assert not block_exists(content, "missing")
    assert count_exact_pattern(content, "same") == 2
    assert count_exact_pattern(content, "Overview", "block-1") == 1


def test_typed_document_compiles_rich_blocks_and_escapes_text() -> None:
    document = DocumentSpec(
        title="AI & Feishu",
        blocks=[
            HeadingBlock(
                type="heading",
                level=1,
                content=[
                    RichTextSpan(
                        text="Outcome < first",
                        bold=True,
                        link="https://example.com/plan?a=1&b=2",
                    )
                ],
            ),
            CalloutBlock(
                type="callout",
                content=[RichTextSpan(text="Use native blocks")],
            ),
            TableBlock(
                type="table",
                headers=[TableCell(content=[RichTextSpan(text="Capability")])],
                rows=[[TableCell(content=[RichTextSpan(text="Documents")])]],
                column_widths=[180],
            ),
            CodeBlock(type="code", language="python", code='print("<safe>")'),
            WhiteboardBlock(type="whiteboard", format=DiagramFormat.MERMAID, source="A --> B"),
        ],
    )

    xml = compile_document(document)

    assert "<title>AI &amp; Feishu</title>" in xml
    assert '<a href="https://example.com/plan?a=1&amp;b=2"><b>' in xml
    assert "Outcome &lt; first" in xml
    assert '<pre lang="python"><code>print(&quot;&lt;safe&gt;&quot;)</code></pre>' in xml
    assert '<whiteboard type="mermaid">A --&gt; B</whiteboard>' in xml
