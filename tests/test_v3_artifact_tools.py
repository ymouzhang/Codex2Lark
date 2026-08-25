from __future__ import annotations

from typing import Any

import pytest

from codex2lark.capabilities.artifacts.tools import (
    CreateBaseTool,
    CreateWorkbookTool,
    RenderWhiteboardTool,
    UpsertBaseRecordsTool,
    WriteSheetTool,
)
from codex2lark.capabilities.base.plugin import FeishuBasePlugin
from codex2lark.capabilities.drive.plugin import FeishuDrivePlugin
from codex2lark.capabilities.sheets.plugin import FeishuSheetsPlugin
from codex2lark.capabilities.whiteboard.plugin import FeishuWhiteboardPlugin
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
    service = FakeArtifactService()
    plugins = (
        FeishuWhiteboardPlugin(service, Identity.USER),  # type: ignore[arg-type]
        FeishuSheetsPlugin(service, Identity.USER),  # type: ignore[arg-type]
        FeishuBasePlugin(service, Identity.USER),  # type: ignore[arg-type]
    )
    tools = [tool for plugin in plugins for tool in plugin.tools]

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


async def test_authoring_plugins_have_independent_manifests_and_lifecycles() -> None:
    service = FakeArtifactService()
    plugins = (
        FeishuDrivePlugin(service),  # type: ignore[arg-type]
        FeishuSheetsPlugin(service, Identity.USER),  # type: ignore[arg-type]
        FeishuBasePlugin(service, Identity.USER),  # type: ignore[arg-type]
        FeishuWhiteboardPlugin(service, Identity.USER),  # type: ignore[arg-type]
    )

    assert {plugin.manifest.plugin_id for plugin in plugins} == {
        "feishu-drive",
        "feishu-sheets",
        "feishu-base",
        "feishu-whiteboard",
    }
    assert sum(len(getattr(plugin, "tools", ())) for plugin in plugins) == 5
    for plugin in plugins:
        assert not (await plugin.health()).healthy
        await plugin.initialize()
        assert (await plugin.health()).healthy
    await plugins[1].stop()
    assert not (await plugins[1].health()).healthy
    assert (await plugins[2].health()).healthy


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


async def test_artifact_tools_resolve_logical_and_exact_write_targets() -> None:
    service = FakeArtifactService()
    workbook = CreateWorkbookTool(service, Identity.USER)  # type: ignore[arg-type]
    workbook_declaration = await workbook.resolve_delegation_target(
        {"resource": " Project Plan "}, context()
    )
    workbook_actual = await workbook.resolve_write_target(
        {
            "title": "project plan",
            "sheets": [
                {
                    "name": "Data",
                    "columns": ["Value"],
                    "data_json": "[[1]]",
                    "dtypes_json": "{}",
                    "formats_json": "{}",
                }
            ],
            "styles_json": "[]",
        },
        context(),
    )
    sheet = WriteSheetTool(service, Identity.USER)  # type: ignore[arg-type]
    sheet_target = await sheet.resolve_write_target(
        {
            "spreadsheet_token": "sheet-1",
            "sheet_id": "tab-1",
            "range": "A1:A1",
            "cells_json": '[[{"value":1}]]',
            "allow_overwrite": True,
        },
        context(),
    )
    base = CreateBaseTool(service, Identity.USER)  # type: ignore[arg-type]
    base_target = await base.resolve_delegation_target({"resource": "Roadmap"}, context())
    upsert = UpsertBaseRecordsTool(service, Identity.USER)  # type: ignore[arg-type]
    upsert_target = await upsert.resolve_delegation_target(
        {"resource": "base-1:table-1"}, context()
    )

    assert workbook_declaration == workbook_actual
    assert workbook_actual.resource_id.startswith("logical:")
    assert sheet_target.resource_id == "sheet-1"
    assert base_target.resource_type == "base-create"
    assert upsert_target.resource_id == "base-1:table-1"
