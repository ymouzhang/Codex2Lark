from __future__ import annotations

from dataclasses import replace

import pytest

from codex2lark.application import create_application
from codex2lark.mcp_server import build_mcp


class FakeEvents:
    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_mcp_registers_semantic_tools_with_write_annotations() -> None:
    server = build_mcp()
    tools = {tool.name: tool for tool in await server.list_tools()}

    assert server.name == "Codex2Lark"
    assert set(tools) == {
        "feishu_chat_digest_publish",
        "feishu_docs_search",
        "feishu_docs_inspect",
        "feishu_docs_create",
        "feishu_docs_publish",
        "feishu_docs_edit",
        "feishu_docs_verify",
        "feishu_whiteboard_render",
        "feishu_sheets_create",
        "feishu_sheets_write",
        "feishu_base_create",
        "feishu_base_upsert_records",
    }
    assert tools["feishu_docs_inspect"].annotations is not None
    assert tools["feishu_docs_inspect"].annotations.readOnlyHint is True
    assert tools["feishu_docs_search"].annotations is not None
    assert tools["feishu_docs_search"].annotations.readOnlyHint is True
    assert tools["feishu_docs_edit"].annotations is not None
    assert tools["feishu_docs_edit"].annotations.destructiveHint is True


@pytest.mark.asyncio
async def test_mcp_lifespan_starts_and_stops_bot_added_events() -> None:
    events = FakeEvents()
    application = replace(create_application(), events=events)  # type: ignore[arg-type]
    server = build_mcp(application)
    lifespan = server.settings.lifespan

    assert lifespan is not None
    async with lifespan(server):
        assert events.started is True
        assert events.stopped is False

    assert events.stopped is True
