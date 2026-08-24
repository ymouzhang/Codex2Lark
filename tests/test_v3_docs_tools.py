from __future__ import annotations

from typing import Any

import pytest

from codex2lark.capabilities.docs.plugin import FeishuDocsPlugin
from codex2lark.capabilities.docs.tools import (
    CreateDocumentTool,
    EditDocumentTool,
    InspectDocumentTool,
    SearchDocumentsTool,
    document_tools,
)
from codex2lark.core.models import CreateDocumentRequest, Identity
from codex2lark.runtime.tools import (
    PolicyDecision,
    ToolContext,
    ToolExecutor,
    ToolRegistry,
    WriteScopeTarget,
)
from codex2lark.runtime.types import ToolCall, ToolDefinition, VerificationState


class FakeDocsService:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def search(self, request: object) -> dict[str, Any]:
        self.requests.append(request)
        return {"ok": True, "matches": []}

    async def inspect(self, request: object) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "ok": True,
            "resource": {"url": "https://example.feishu.cn/docx/docx_1"},
            "data": {"content": "sensitive live body"},
            "revision": 3,
        }

    async def create(self, request: object) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "ok": True,
            "resource": {"url": "https://example.feishu.cn/docx/docx_2"},
            "verification": {"status": "passed"},
        }

    async def edit(self, request: object) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "ok": True,
            "resource": {"url": "https://example.feishu.cn/docx/docx_3"},
            "verification": {"status": "passed"},
        }


def context() -> ToolContext:
    return ToolContext("run", "/root", "tenant", "app", "user", "session", "user", 1)


class Allow:
    async def authorize(
        self, definition: ToolDefinition, call: ToolCall, context: ToolContext
    ) -> PolicyDecision:
        del definition, call, context
        return PolicyDecision(True, "test")


class NoApproval:
    async def request(
        self, definition: ToolDefinition, call: ToolCall, context: ToolContext
    ) -> bool:
        del definition, call, context
        return False


def test_document_tool_profile_has_strict_schemas_without_identity_override() -> None:
    tools = document_tools(FakeDocsService(), Identity.USER)  # type: ignore[arg-type]

    assert [tool.definition.tool_id for tool in tools] == [
        "feishu.docs.search",
        "feishu.docs.inspect",
        "feishu.docs.create",
        "feishu.docs.edit",
    ]
    for tool in tools:
        schema = tool.definition.input_schema
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert "identity" not in schema["properties"]


async def test_document_plugin_manifest_and_lifecycle() -> None:
    plugin = FeishuDocsPlugin(FakeDocsService(), Identity.USER)  # type: ignore[arg-type]

    assert plugin.manifest.plugin_id == "feishu-docs"
    assert len(plugin.tools) == 4
    assert not (await plugin.health()).healthy
    await plugin.initialize()
    assert (await plugin.health()).healthy
    await plugin.stop()


async def test_inspect_returns_live_content_but_marks_it_nonpersistable() -> None:
    service = FakeDocsService()
    tool = InspectDocumentTool(service, Identity.USER)  # type: ignore[arg-type]
    arguments = {"resource": "https://example.feishu.cn/docx/docx_1"}

    tool.validate(arguments)
    result = await tool.execute(arguments, context())
    verification = await tool.verify(arguments, result, context())

    assert "sensitive live body" in str(result)
    assert tool.checkpoint_safe_observation is False
    assert verification.state is VerificationState.NOT_REQUIRED


async def test_create_binds_operator_identity_and_returns_verified_resource() -> None:
    service = FakeDocsService()
    tool = CreateDocumentTool(service, Identity.BOT)  # type: ignore[arg-type]
    arguments = {
        "title": "Architecture",
        "content_xml": "<title>Architecture</title><p>Body</p>",
        "required_text": ["Body"],
    }

    tool.validate(arguments)
    result = await tool.execute(arguments, context())
    verification = await tool.verify(arguments, result, context())

    request = service.requests[0]
    assert isinstance(request, CreateDocumentRequest)
    assert request.identity is Identity.BOT
    assert verification.state is VerificationState.VERIFIED
    assert verification.resource_refs == ("https://example.feishu.cn/docx/docx_2",)


async def test_create_reconciliation_finds_and_verifies_existing_live_document() -> None:
    class ReconcileDocs(FakeDocsService):
        async def search(self, request: object) -> dict[str, Any]:
            self.requests.append(request)
            return {
                "ok": True,
                "scope": "managed_folder",
                "matches": [
                    {
                        "title": "Architecture",
                        "url": "https://example.feishu.cn/docx/docx_existing",
                    }
                ],
            }

        async def inspect(self, request: object) -> dict[str, Any]:
            self.requests.append(request)
            return {
                "ok": True,
                "resource": {"url": "https://example.feishu.cn/docx/docx_existing"},
                "data": {"content": "Architecture body is present"},
                "revision": 4,
            }

    tool = CreateDocumentTool(ReconcileDocs(), Identity.USER)  # type: ignore[arg-type]
    result = await tool.reconcile(
        {
            "title": "Architecture",
            "content_xml": "<title>Architecture</title><p>body is present</p>",
            "required_text": ["body is present"],
        },
        context(),
    )

    assert result.verification.state is VerificationState.VERIFIED
    assert result.verification.resource_refs == ("https://example.feishu.cn/docx/docx_existing",)


def test_edit_rejects_ambiguous_target_before_service_call() -> None:
    tool = EditDocumentTool(FakeDocsService(), Identity.USER)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exactly one"):
        tool.validate(
            {
                "resource": "docx_1",
                "document_title": "Architecture",
                "command": "append",
                "content_xml": "<p>More</p>",
                "pattern": None,
                "block_id": None,
                "change_summary": "Add details",
                "required_text": ["More"],
            }
        )


def test_search_rejects_unknown_arguments() -> None:
    tool = SearchDocumentsTool(FakeDocsService(), Identity.USER)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        tool.validate({"title": "Architecture", "identity": "bot"})


async def test_delegated_document_edit_enforces_live_locked_target_and_revision() -> None:
    service = FakeDocsService()
    tool = EditDocumentTool(service, Identity.USER)  # type: ignore[arg-type]
    registry = ToolRegistry([tool])
    executor = ToolExecutor(registry, Allow(), NoApproval())
    arguments = {
        "resource": "docx_1",
        "document_title": None,
        "command": "append",
        "content_xml": "<p>More</p>",
        "pattern": None,
        "block_id": None,
        "change_summary": "Add details",
        "required_text": ["More"],
    }
    call = ToolCall("call-1", "feishu.docs.edit", arguments)

    denied = await executor.execute(
        call,
        ToolContext(
            "run",
            "/root/writer",
            "tenant",
            "app",
            "user",
            "session",
            "user",
            1,
            write_scope=(WriteScopeTarget("docx", "docx_other", "3"),),
        ),
    )
    expired = await executor.execute(
        call,
        ToolContext(
            "run",
            "/root/writer",
            "tenant",
            "app",
            "user",
            "session",
            "user",
            1,
            write_scope_required=True,
        ),
    )
    allowed = await executor.execute(
        call,
        ToolContext(
            "run",
            "/root/writer",
            "tenant",
            "app",
            "user",
            "session",
            "user",
            1,
            write_scope=(WriteScopeTarget("docx", "docx_1", "3"),),
        ),
    )

    assert denied.error_code == "write_scope_violation"
    assert expired.error_code == "write_lock_missing"
    assert allowed.succeeded
    assert allowed.verification.state is VerificationState.VERIFIED
