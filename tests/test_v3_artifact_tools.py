from __future__ import annotations

from typing import Any

import pytest

from codex2lark.capabilities.artifacts.plugin import FeishuArtifactsPlugin
from codex2lark.capabilities.artifacts.tools import (
    CreateWorkbookTool,
    RenderWhiteboardTool,
    UpsertBaseRecordsTool,
    artifact_tools,
)
from codex2lark.core.models import CreateWorkbookRequest, Identity
from codex2lark.runtime.tools import ToolContext
from codex2lark.runtime.types import VerificationState


class FakeArtifactService:
    def __init__(self) -> None:
        self.requests: list[object] = []

    async def render_whiteboard(self, request: object) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "resource": {"whiteboard_token": "board_1"},
            "verification": {"status": "upstream_confirmed"},
        }

    async def create_workbook(self, request: object) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "resource": {"spreadsheet_token": "sheet_1"},
            "verification": {"status": "upstream_confirmed"},
        }

    async def write_sheet(self, request: object) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "resource": {"spreadsheet_token": "sheet_1"},
            "verification": {"status": "passed"},
        }

    async def create_base(self, request: object) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "resource": {"base_token": "base_1"},
            "verification": {"status": "upstream_confirmed"},
        }

    async def upsert_base_records(self, request: object) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "resource": {"base_token": "base_1"},
            "verification": {"status": "upstream_confirmed"},
        }


def context() -> ToolContext:
    return ToolContext("run", "/root", "tenant", "app", "user", "session", "user", 1)


def test_artifact_tool_profile_uses_strict_bounded_schemas() -> None:
    tools = artifact_tools(FakeArtifactService(), Identity.USER)  # type: ignore[arg-type]

    assert [tool.definition.tool_id for tool in tools] == [
        "feishu.whiteboard.render",
        "feishu.sheets.create",
        "feishu.sheets.write",
        "feishu.base.create",
        "feishu.base.upsert",
    ]
    for tool in tools:
        schema = tool.definition.input_schema
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        assert "identity" not in schema["properties"]
        assert tool.checkpoint_safe_observation is False


async def test_artifact_plugin_manifest_and_lifecycle() -> None:
    plugin = FeishuArtifactsPlugin(
        FakeArtifactService(),
        Identity.USER,  # type: ignore[arg-type]
    )

    assert plugin.manifest.plugin_id == "feishu-artifacts"
    assert len(plugin.tools) == 5
    assert not (await plugin.health()).healthy
    await plugin.initialize()
    assert (await plugin.health()).healthy
    await plugin.stop()


async def test_workbook_tool_parses_json_and_binds_identity() -> None:
    service = FakeArtifactService()
    tool = CreateWorkbookTool(service, Identity.BOT)  # type: ignore[arg-type]
    arguments = {
        "title": "Plan",
        "sheets": [
            {
                "name": "Milestones",
                "columns": ["Name", "Owner"],
                "data_json": '[["Design","Aaron"]]',
                "dtypes_json": "{}",
                "formats_json": "{}",
            }
        ],
        "styles_json": "[]",
    }

    tool.validate(arguments)
    observation = await tool.execute(arguments, context())
    verification = await tool.verify(arguments, observation, context())

    request = service.requests[0]
    assert isinstance(request, CreateWorkbookRequest)
    assert request.identity is Identity.BOT
    assert request.sheets[0].data == [["Design", "Aaron"]]
    assert verification.state is VerificationState.VERIFIED
    assert verification.resource_refs == ("sheet_1",)


def test_base_records_tool_rejects_non_array_json() -> None:
    tool = UpsertBaseRecordsTool(
        FakeArtifactService(),
        Identity.USER,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="decode to list"):
        tool.validate(
            {
                "base_token": "base_1",
                "table_id": "tbl_1",
                "records_json": '{"not":"an array"}',
                "mode": "create",
            }
        )


def test_whiteboard_tool_requires_target_matching_mode() -> None:
    tool = RenderWhiteboardTool(
        FakeArtifactService(),
        Identity.USER,  # type: ignore[arg-type]
    )
    with pytest.raises(ValueError, match="document is required"):
        tool.validate(
            {
                "mode": "create",
                "format": "mermaid",
                "source": "flowchart LR\nA-->B",
                "document": None,
                "whiteboard_token": None,
                "anchor_block_id": None,
                "overwrite": True,
            }
        )
