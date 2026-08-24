from __future__ import annotations

from html import escape
from xml.etree import ElementTree

from .errors import Codex2LarkError, ErrorCategory
from .models import (
    BookmarkBlock,
    CalloutBlock,
    CheckboxBlock,
    CodeBlock,
    DiagramFormat,
    DividerBlock,
    DocumentFormat,
    DocumentSpec,
    HeadingBlock,
    ImageBlock,
    ListBlock,
    ParagraphBlock,
    QuoteBlock,
    RichText,
    RichTextSpan,
    SheetBlock,
    TableBlock,
    TableCell,
    WhiteboardBlock,
)

SUPPORTED_TAGS = {
    "a",
    "b",
    "blockquote",
    "bookmark",
    "br",
    "button",
    "callout",
    "checkbox",
    "cite",
    "code",
    "col",
    "colgroup",
    "column",
    "del",
    "em",
    "figure",
    "grid",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "h7",
    "h8",
    "h9",
    "hr",
    "img",
    "latex",
    "li",
    "ol",
    "p",
    "pre",
    "sheet",
    "source",
    "span",
    "sub-page-list",
    "table",
    "tbody",
    "td",
    "th",
    "thead",
    "time",
    "title",
    "tr",
    "u",
    "ul",
    "whiteboard",
}


def preflight_content(content: str, format: DocumentFormat) -> None:
    if not content.strip():
        raise Codex2LarkError(ErrorCategory.VALIDATION, "document content cannot be empty")
    if format is DocumentFormat.MARKDOWN:
        return
    lowered = content.lower()
    if "<!doctype" in lowered or "<!entity" in lowered:
        raise Codex2LarkError(
            ErrorCategory.VALIDATION,
            "DOCTYPE and ENTITY declarations are not allowed in Feishu XML",
        )
    try:
        root = ElementTree.fromstring(f"<codex2lark-fragment>{content}</codex2lark-fragment>")
    except ElementTree.ParseError as exc:
        raise Codex2LarkError(
            ErrorCategory.VALIDATION,
            "invalid Feishu XML",
            details={"reason": str(exc)},
        ) from exc
    unsupported = sorted(
        {
            element.tag
            for element in root.iter()
            if element is not root and element.tag not in SUPPORTED_TAGS
        }
    )
    if unsupported:
        raise Codex2LarkError(
            ErrorCategory.VALIDATION,
            "Feishu XML contains unsupported tags",
            details={"tags": unsupported},
        )


def _fragment_root(content: str) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(f"<codex2lark-fragment>{content}</codex2lark-fragment>")
    except ElementTree.ParseError as exc:
        raise Codex2LarkError(
            ErrorCategory.UPSTREAM,
            "live Feishu XML could not be parsed for precise editing",
            details={"reason": str(exc)},
        ) from exc


def block_exists(content: str, block_id: str) -> bool:
    root = _fragment_root(content)
    return any(
        element.get("id") == block_id or element.get("block-id") == block_id
        for element in root.iter()
    )


def count_exact_pattern(content: str, pattern: str, block_id: str | None = None) -> int:
    if block_id is None:
        return content.count(pattern)
    root = _fragment_root(content)
    for element in root.iter():
        if element.get("id") == block_id or element.get("block-id") == block_id:
            serialized = ElementTree.tostring(element, encoding="unicode")
            raw_count = serialized.count(pattern)
            return raw_count if raw_count else "".join(element.itertext()).count(pattern)
    return 0


def whiteboard_xml(format: DiagramFormat, source: str) -> str:
    return f'<whiteboard type="{format.value}">{escape(source)}</whiteboard>'


def _attributes(values: dict[str, str | int | None]) -> str:
    rendered = [
        f' {name}="{escape(str(value), quote=True)}"'
        for name, value in values.items()
        if value is not None
    ]
    return "".join(rendered)


def _render_span(span: RichTextSpan) -> str:
    content = escape(span.text).replace("\n", "<br/>")
    if span.formula:
        return f"<latex>{content}</latex>"

    if span.text_color is not None or span.background_color is not None:
        attributes = _attributes(
            {
                "text-color": span.text_color,
                "background-color": span.background_color,
            }
        )
        content = f"<span{attributes}>{content}</span>"
    if span.inline_code:
        content = f"<code>{content}</code>"
    if span.underline:
        content = f"<u>{content}</u>"
    if span.strike:
        content = f"<del>{content}</del>"
    if span.emphasis:
        content = f"<em>{content}</em>"
    if span.bold:
        content = f"<b>{content}</b>"
    if span.link is not None:
        content = f'<a href="{escape(span.link, quote=True)}">{content}</a>'
    return content


def _render_rich_text(content: RichText) -> str:
    return "".join(_render_span(span) for span in content)


def _render_cell(cell: TableCell, tag: str) -> str:
    attributes = _attributes(
        {
            "background-color": cell.background_color,
            "vertical-align": cell.vertical_align,
            "colspan": cell.colspan if cell.colspan > 1 else None,
            "rowspan": cell.rowspan if cell.rowspan > 1 else None,
        }
    )
    return f"<{tag}{attributes}>{_render_rich_text(cell.content)}</{tag}>"


def _render_table(block: TableBlock) -> str:
    parts = ["<table>"]
    if block.column_widths:
        parts.append("<colgroup>")
        parts.extend(f'<col width="{width}"/>' for width in block.column_widths)
        parts.append("</colgroup>")
    if block.headers:
        parts.append("<thead><tr>")
        parts.extend(_render_cell(cell, "th") for cell in block.headers)
        parts.append("</tr></thead>")
    parts.append("<tbody>")
    for row in block.rows:
        parts.append("<tr>")
        parts.extend(_render_cell(cell, "td") for cell in row)
        parts.append("</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def _render_block(block: object) -> str:
    if isinstance(block, HeadingBlock):
        return (
            f'<h{block.level} align="{block.align}">'
            f"{_render_rich_text(block.content)}</h{block.level}>"
        )
    if isinstance(block, ParagraphBlock):
        return f'<p align="{block.align}">{_render_rich_text(block.content)}</p>'
    if isinstance(block, ListBlock):
        tag = "ul" if block.type == "bullet_list" else "ol"
        items = "".join(f"<li>{_render_rich_text(item)}</li>" for item in block.items)
        return f"<{tag}>{items}</{tag}>"
    if isinstance(block, CheckboxBlock):
        done = str(block.done).lower()
        return f'<checkbox done="{done}">{_render_rich_text(block.content)}</checkbox>'
    if isinstance(block, QuoteBlock):
        return f"<blockquote><p>{_render_rich_text(block.content)}</p></blockquote>"
    if isinstance(block, CalloutBlock):
        attributes = _attributes(
            {
                "emoji": block.emoji,
                "background-color": block.background_color,
                "border-color": block.border_color,
                "text-color": block.text_color,
            }
        )
        return f"<callout{attributes}><p>{_render_rich_text(block.content)}</p></callout>"
    if isinstance(block, CodeBlock):
        attributes = _attributes({"lang": block.language, "caption": block.caption})
        return f"<pre{attributes}><code>{escape(block.code)}</code></pre>"
    if isinstance(block, DividerBlock):
        return "<hr/>"
    if isinstance(block, TableBlock):
        return _render_table(block)
    if isinstance(block, ImageBlock):
        attributes = _attributes(
            {
                "href": block.url,
                "caption": block.caption,
                "name": block.name,
                "width": block.width,
                "height": block.height,
            }
        )
        return f"<img{attributes}/>"
    if isinstance(block, BookmarkBlock):
        attributes = _attributes({"name": block.name, "href": block.url})
        return f"<bookmark{attributes}></bookmark>"
    if isinstance(block, WhiteboardBlock):
        return whiteboard_xml(block.format, block.source)
    if isinstance(block, SheetBlock):
        if block.token is None:
            return '<sheet type="blank"></sheet>'
        attributes = _attributes({"token": block.token, "sheet-id": block.sheet_id})
        return f"<sheet{attributes}></sheet>"
    raise TypeError(f"unsupported document block: {type(block).__name__}")


def compile_document(document: DocumentSpec) -> str:
    content = "\n".join(
        [f"<title>{escape(document.title)}</title>", *map(_render_block, document.blocks)]
    )
    preflight_content(content, DocumentFormat.XML)
    return content
