from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings as FastMCPSettings
from mcp.types import ToolAnnotations

from .application import Application, create_application
from .lark_cli import safe_tool_call_error
from .models import (
    ChatDigestRequest,
    CreateBaseRequest,
    CreateDocumentRequest,
    CreateWorkbookRequest,
    EditDocumentRequest,
    InspectDocumentRequest,
    PublishDocumentRequest,
    SearchDocumentsRequest,
    UpsertBaseRecordsRequest,
    VerifyDocumentRequest,
    WhiteboardRenderRequest,
    WriteSheetRequest,
)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
CREATE_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=True,
)
MUTATING_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=True,
)

# MCP 1.29 leaves the lifespan annotation unresolved until an explicit rebuild,
# which makes pydantic-settings warn on every server startup.
FastMCPSettings.model_rebuild()


def build_mcp(application: Application | None = None) -> FastMCP:
    app = application or create_application()
    mcp = FastMCP(
        "Codex2Lark",
        instructions=(
            "Stateless Feishu authoring tools. Inspect live resources before editing and verify "
            "all writes. Feishu is the only business-data source of truth."
        ),
    )

    @mcp.tool(
        name="feishu_chat_digest_publish",
        description=(
            "Resolve one Feishu group, fetch a complete bounded time range, and publish a "
            "chronological sender-aware document. Images are inserted from ephemeral downloads; "
            "file attachments are never downloaded. With bot identity, the current authenticated "
            "user is added to the group when absent and membership is verified before reading. "
            "This may add a group member and creates an external document."
        ),
        annotations=CREATE_WRITE,
    )
    async def feishu_chat_digest_publish(request: ChatDigestRequest) -> dict[str, Any]:
        try:
            return await app.chat_digest.publish(request)
        except Exception as exc:
            return safe_tool_call_error(exc)

    @mcp.tool(
        name="feishu_docs_search",
        description=(
            "Find Feishu documents by exact title, preferring the managed Codex2Lark folder. "
            "This is read-only and should precede title-based edits."
        ),
        annotations=READ_ONLY,
    )
    async def feishu_docs_search(request: SearchDocumentsRequest) -> dict[str, Any]:
        try:
            return await app.docs.search(request)
        except Exception as exc:
            return safe_tool_call_error(exc)

    @mcp.tool(
        name="feishu_docs_inspect",
        description="Read a live Feishu document. This is read-only and should precede edits.",
        annotations=READ_ONLY,
    )
    async def feishu_docs_inspect(request: InspectDocumentRequest) -> dict[str, Any]:
        try:
            return await app.docs.inspect(request)
        except Exception as exc:  # MCP boundary normalizes all implementation failures.
            return safe_tool_call_error(exc)

    @mcp.tool(
        name="feishu_docs_create",
        description=(
            "Create and read-back verify a Feishu document. This performs an external write and "
            "requires clear user intent."
        ),
        annotations=CREATE_WRITE,
    )
    async def feishu_docs_create(request: CreateDocumentRequest) -> dict[str, Any]:
        try:
            return await app.docs.create(request)
        except Exception as exc:
            return safe_tool_call_error(exc)

    @mcp.tool(
        name="feishu_docs_publish",
        description=(
            "Compile a typed rich-document specification, create the Feishu document, and "
            "verify it by reading the live resource back. This performs an external write."
        ),
        annotations=CREATE_WRITE,
    )
    async def feishu_docs_publish(request: PublishDocumentRequest) -> dict[str, Any]:
        try:
            return await app.docs.publish(request)
        except Exception as exc:
            return safe_tool_call_error(exc)

    @mcp.tool(
        name="feishu_docs_edit",
        description=(
            "Resolve a URL/token or exact title, apply bounded operations, verify the live "
            "result, and notify the current user as the Feishu bot. This performs an external "
            "write."
        ),
        annotations=MUTATING_WRITE,
    )
    async def feishu_docs_edit(request: EditDocumentRequest) -> dict[str, Any]:
        try:
            return await app.docs.edit(request)
        except Exception as exc:
            return safe_tool_call_error(exc)

    @mcp.tool(
        name="feishu_docs_verify",
        description="Read a Feishu document and evaluate semantic and structural invariants.",
        annotations=READ_ONLY,
    )
    async def feishu_docs_verify(request: VerifyDocumentRequest) -> dict[str, Any]:
        try:
            return await app.docs.verify(request)
        except Exception as exc:
            return safe_tool_call_error(exc)

    @mcp.tool(
        name="feishu_whiteboard_render",
        description=(
            "Create a document whiteboard or update one from Mermaid, PlantUML, or SVG. "
            "This performs an external write."
        ),
        annotations=MUTATING_WRITE,
    )
    async def feishu_whiteboard_render(request: WhiteboardRenderRequest) -> dict[str, Any]:
        try:
            return await app.artifacts.render_whiteboard(request)
        except Exception as exc:
            return safe_tool_call_error(exc)

    @mcp.tool(
        name="feishu_sheets_create",
        description="Create a typed Feishu workbook. This performs an external write.",
        annotations=CREATE_WRITE,
    )
    async def feishu_sheets_create(request: CreateWorkbookRequest) -> dict[str, Any]:
        try:
            return await app.artifacts.create_workbook(request)
        except Exception as exc:
            return safe_tool_call_error(exc)

    @mcp.tool(
        name="feishu_sheets_write",
        description=(
            "Write typed cells to a bounded Sheet range and read formulas back. "
            "This performs an external write."
        ),
        annotations=MUTATING_WRITE,
    )
    async def feishu_sheets_write(request: WriteSheetRequest) -> dict[str, Any]:
        try:
            return await app.artifacts.write_sheet(request)
        except Exception as exc:
            return safe_tool_call_error(exc)

    @mcp.tool(
        name="feishu_base_create",
        description="Create a Feishu Base and optional tables. This performs an external write.",
        annotations=CREATE_WRITE,
    )
    async def feishu_base_create(request: CreateBaseRequest) -> dict[str, Any]:
        try:
            return await app.artifacts.create_base(request)
        except Exception as exc:
            return safe_tool_call_error(exc)

    @mcp.tool(
        name="feishu_base_upsert_records",
        description=(
            "Create or update a bounded record batch in Feishu Base. "
            "This performs an external write."
        ),
        annotations=MUTATING_WRITE,
    )
    async def feishu_base_upsert_records(
        request: UpsertBaseRecordsRequest,
    ) -> dict[str, Any]:
        try:
            return await app.artifacts.upsert_base_records(request)
        except Exception as exc:
            return safe_tool_call_error(exc)

    return mcp


def run_stdio() -> None:
    build_mcp().run(transport="stdio")
