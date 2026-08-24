from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Identity(StrEnum):
    USER = "user"
    BOT = "bot"


class DocumentFormat(StrEnum):
    XML = "xml"
    MARKDOWN = "markdown"


class DetailLevel(StrEnum):
    SIMPLE = "simple"
    WITH_IDS = "with_ids"
    FULL = "full"


class DiagramFormat(StrEnum):
    MERMAID = "mermaid"
    PLANTUML = "plantuml"
    SVG = "svg"


class ResourceRef(StrictModel):
    url: str | None = None
    token: str | None = None

    @model_validator(mode="after")
    def exactly_one_reference(self) -> ResourceRef:
        if (self.url is None) == (self.token is None):
            raise ValueError("exactly one of url or token is required")
        return self

    @property
    def value(self) -> str:
        value = self.url or self.token
        assert value is not None
        return value


class VerificationPolicy(StrictModel):
    expected_title: str | None = None
    required_text: list[str] = Field(default_factory=list, max_length=100)
    forbidden_text: list[str] = Field(default_factory=list, max_length=100)
    protected_text: list[str] = Field(default_factory=list, max_length=100)
    min_blocks: dict[str, Annotated[int, Field(ge=0, le=10_000)]] = Field(default_factory=dict)
    fail_on_warning: bool = False


Alignment = Literal["left", "center", "right"]
NamedColor = Literal[
    "red",
    "orange",
    "yellow",
    "green",
    "blue",
    "purple",
    "gray",
    "light-red",
    "light-orange",
    "light-yellow",
    "light-green",
    "light-blue",
    "light-purple",
    "light-gray",
    "medium-gray",
]


class RichTextSpan(StrictModel):
    text: str = Field(min_length=1, max_length=100_000)
    bold: bool = False
    emphasis: bool = False
    strike: bool = False
    underline: bool = False
    inline_code: bool = False
    formula: bool = False
    link: str | None = Field(default=None, max_length=4096)
    text_color: NamedColor | None = None
    background_color: NamedColor | None = None

    @model_validator(mode="after")
    def validate_span(self) -> RichTextSpan:
        if self.link is not None and not self.link.startswith("https://"):
            raise ValueError("rich-text links must use HTTPS")
        styled = any(
            (
                self.bold,
                self.emphasis,
                self.strike,
                self.underline,
                self.inline_code,
                self.link is not None,
                self.text_color is not None,
                self.background_color is not None,
            )
        )
        if self.formula and styled:
            raise ValueError("formula spans cannot also use text styles or links")
        return self


RichText = list[RichTextSpan]


class HeadingBlock(StrictModel):
    type: Literal["heading"]
    level: int = Field(ge=1, le=9)
    content: RichText = Field(min_length=1, max_length=100)
    align: Alignment = "left"


class ParagraphBlock(StrictModel):
    type: Literal["paragraph"]
    content: RichText = Field(min_length=1, max_length=200)
    align: Alignment = "left"


class ListBlock(StrictModel):
    type: Literal["bullet_list", "ordered_list"]
    items: list[RichText] = Field(min_length=1, max_length=200)


class CheckboxBlock(StrictModel):
    type: Literal["checkbox"]
    content: RichText = Field(min_length=1, max_length=100)
    done: bool = False


class QuoteBlock(StrictModel):
    type: Literal["quote"]
    content: RichText = Field(min_length=1, max_length=100)


class CalloutBlock(StrictModel):
    type: Literal["callout"]
    content: RichText = Field(min_length=1, max_length=100)
    emoji: str = Field(default="💡", min_length=1, max_length=8)
    background_color: NamedColor = "light-yellow"
    border_color: NamedColor = "yellow"
    text_color: NamedColor | None = None


class CodeBlock(StrictModel):
    type: Literal["code"]
    code: str = Field(min_length=1, max_length=1_000_000)
    language: str = Field(default="text", min_length=1, max_length=50, pattern=r"^[\w+.-]+$")
    caption: str | None = Field(default=None, max_length=500)


class DividerBlock(StrictModel):
    type: Literal["divider"]


class TableCell(StrictModel):
    content: RichText = Field(default_factory=list, max_length=100)
    background_color: NamedColor | None = None
    vertical_align: Literal["top", "middle", "bottom"] = "top"
    colspan: int = Field(default=1, ge=1, le=50)
    rowspan: int = Field(default=1, ge=1, le=1000)


class TableBlock(StrictModel):
    type: Literal["table"]
    headers: list[TableCell] = Field(default_factory=list, max_length=50)
    rows: list[list[TableCell]] = Field(min_length=1, max_length=1000)
    column_widths: list[int] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def validate_table_shape(self) -> TableBlock:
        logical_widths = [sum(cell.colspan for cell in row) for row in self.rows]
        if self.headers:
            logical_widths.append(sum(cell.colspan for cell in self.headers))
        if len(set(logical_widths)) != 1:
            raise ValueError("all table rows must have the same logical width")
        width = logical_widths[0]
        if self.column_widths and len(self.column_widths) != width:
            raise ValueError("column_widths must match the table's logical width")
        return self


class ImageBlock(StrictModel):
    type: Literal["image"]
    url: str = Field(max_length=4096)
    caption: str | None = Field(default=None, max_length=500)
    name: str | None = Field(default=None, max_length=500)
    width: int | None = Field(default=None, ge=1, le=10_000)
    height: int | None = Field(default=None, ge=1, le=10_000)

    @model_validator(mode="after")
    def validate_url(self) -> ImageBlock:
        if not self.url.startswith("https://"):
            raise ValueError("image URLs must use HTTPS")
        return self


class BookmarkBlock(StrictModel):
    type: Literal["bookmark"]
    name: str = Field(min_length=1, max_length=500)
    url: str = Field(max_length=4096)

    @model_validator(mode="after")
    def validate_url(self) -> BookmarkBlock:
        if not self.url.startswith("https://"):
            raise ValueError("bookmark URLs must use HTTPS")
        return self


class WhiteboardBlock(StrictModel):
    type: Literal["whiteboard"]
    format: DiagramFormat
    source: str = Field(min_length=1, max_length=4_000_000)


class SheetBlock(StrictModel):
    type: Literal["sheet"]
    token: str | None = Field(default=None, max_length=1024)
    sheet_id: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def validate_resource(self) -> SheetBlock:
        if (self.token is None) != (self.sheet_id is None):
            raise ValueError("token and sheet_id must be supplied together")
        return self


DocumentBlock = Annotated[
    HeadingBlock
    | ParagraphBlock
    | ListBlock
    | CheckboxBlock
    | QuoteBlock
    | CalloutBlock
    | CodeBlock
    | DividerBlock
    | TableBlock
    | ImageBlock
    | BookmarkBlock
    | WhiteboardBlock
    | SheetBlock,
    Field(discriminator="type"),
]


class DocumentSpec(StrictModel):
    title: str = Field(min_length=1, max_length=800)
    blocks: list[DocumentBlock] = Field(min_length=1, max_length=2000)


class InspectDocumentRequest(StrictModel):
    resource: ResourceRef
    format: DocumentFormat = DocumentFormat.XML
    detail: DetailLevel = DetailLevel.FULL
    identity: Identity = Identity.USER


class CreateDocumentRequest(StrictModel):
    title: str = Field(min_length=1, max_length=800)
    format: DocumentFormat = DocumentFormat.XML
    content: str = Field(min_length=1, max_length=8_000_000)
    folder_token: str | None = Field(default=None, max_length=1024)
    identity: Identity = Identity.USER
    verification: VerificationPolicy = Field(default_factory=VerificationPolicy)


class PublishDocumentRequest(StrictModel):
    document: DocumentSpec
    folder_token: str | None = Field(default=None, max_length=1024)
    identity: Identity = Identity.USER
    verification: VerificationPolicy = Field(default_factory=VerificationPolicy)


class EditCommand(StrEnum):
    APPEND = "append"
    OVERWRITE = "overwrite"
    STR_REPLACE = "str_replace"
    BLOCK_INSERT_AFTER = "block_insert_after"
    BLOCK_REPLACE = "block_replace"
    BLOCK_DELETE = "block_delete"


class EditOperation(StrictModel):
    command: EditCommand
    content: str | None = Field(default=None, max_length=8_000_000)
    pattern: str | None = Field(default=None, max_length=1_000_000)
    block_id: str | None = Field(default=None, max_length=1024)
    format: DocumentFormat = DocumentFormat.XML

    @model_validator(mode="after")
    def validate_command_fields(self) -> EditOperation:
        content_commands = {
            EditCommand.APPEND,
            EditCommand.OVERWRITE,
            EditCommand.BLOCK_INSERT_AFTER,
            EditCommand.BLOCK_REPLACE,
            EditCommand.STR_REPLACE,
        }
        block_commands = {
            EditCommand.BLOCK_INSERT_AFTER,
            EditCommand.BLOCK_REPLACE,
            EditCommand.BLOCK_DELETE,
        }
        if self.command in content_commands and self.content is None:
            raise ValueError(f"content is required for {self.command.value}")
        if self.command in block_commands and self.block_id is None:
            raise ValueError(f"block_id is required for {self.command.value}")
        if self.command is EditCommand.STR_REPLACE and self.pattern is None:
            raise ValueError("pattern is required for str_replace")
        if self.command is EditCommand.BLOCK_DELETE and self.content is not None:
            raise ValueError("content is not allowed for block_delete")
        return self


class EditDocumentRequest(StrictModel):
    resource: ResourceRef
    operations: list[EditOperation] = Field(min_length=1, max_length=20)
    expected_revision: int | None = Field(default=None, ge=0)
    identity: Identity = Identity.USER
    verification: VerificationPolicy = Field(default_factory=VerificationPolicy)


class VerifyDocumentRequest(StrictModel):
    resource: ResourceRef
    policy: VerificationPolicy
    identity: Identity = Identity.USER


class WhiteboardRenderRequest(StrictModel):
    mode: Literal["create", "update"]
    format: DiagramFormat
    source: str = Field(min_length=1, max_length=4_000_000)
    document: ResourceRef | None = None
    whiteboard_token: str | None = Field(default=None, max_length=1024)
    anchor_block_id: str | None = Field(default=None, max_length=1024)
    overwrite: bool = True
    identity: Identity = Identity.USER

    @model_validator(mode="after")
    def validate_target(self) -> WhiteboardRenderRequest:
        if self.mode == "create" and self.document is None:
            raise ValueError("document is required when creating a whiteboard")
        if self.mode == "update" and not self.whiteboard_token:
            raise ValueError("whiteboard_token is required when updating a whiteboard")
        return self


class SheetSpec(StrictModel):
    name: str = Field(min_length=1, max_length=200)
    columns: list[str] = Field(min_length=1, max_length=500)
    data: list[list[str | int | float | bool | None]] = Field(default_factory=list)
    dtypes: dict[str, str] = Field(default_factory=dict)
    formats: dict[str, str] = Field(default_factory=dict)


class CreateWorkbookRequest(StrictModel):
    title: str = Field(min_length=1, max_length=800)
    sheets: list[SheetSpec] = Field(min_length=1, max_length=50)
    styles: list[dict[str, Any]] = Field(default_factory=list, max_length=50)
    folder_token: str | None = Field(default=None, max_length=1024)
    identity: Identity = Identity.USER


class WriteSheetRequest(StrictModel):
    spreadsheet_token: str = Field(min_length=1, max_length=1024)
    sheet_id: str = Field(min_length=1, max_length=1024)
    range: str = Field(min_length=1, max_length=100)
    cells: list[list[dict[str, Any]]] = Field(min_length=1, max_length=500)
    allow_overwrite: bool = True
    identity: Identity = Identity.USER


class BaseTableSpec(StrictModel):
    name: str = Field(min_length=1, max_length=200)


class CreateBaseRequest(StrictModel):
    name: str = Field(min_length=1, max_length=800)
    tables: list[BaseTableSpec] = Field(default_factory=list, max_length=50)
    folder_token: str | None = Field(default=None, max_length=1024)
    identity: Identity = Identity.USER


class UpsertBaseRecordsRequest(StrictModel):
    base_token: str = Field(min_length=1, max_length=1024)
    table_id: str = Field(min_length=1, max_length=1024)
    records: list[dict[str, Any]] = Field(min_length=1, max_length=500)
    mode: Literal["create", "update"] = "create"
    identity: Identity = Identity.USER
