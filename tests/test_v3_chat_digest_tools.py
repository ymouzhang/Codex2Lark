from __future__ import annotations

from typing import Any

import pytest

from codex2lark.capabilities.chat_digest.plugin import FeishuChatDigestPlugin
from codex2lark.capabilities.chat_digest.tools import PublishChatDigestTool
from codex2lark.core.models import ChatDigestRequest, Identity
from codex2lark.runtime.tools import ToolContext
from codex2lark.runtime.types import VerificationState


class FakeDigestService:
    def __init__(self) -> None:
        self.requests: list[ChatDigestRequest] = []

    async def publish(self, request: ChatDigestRequest) -> dict[str, Any]:
        self.requests.append(request)
        return {
            "ok": True,
            "action": "created",
            "resource": {"url": "https://example.feishu.cn/docx/group_digest"},
            "managed_folder": {"name": "Codex2Lark", "token": "fld_managed"},
            "verification": {"status": "passed"},
        }


def context() -> ToolContext:
    return ToolContext(
        "run", "/root", "tenant", "app", "user", "session", "user", 1, chat_id="oc_group"
    )


def arguments() -> dict[str, object]:
    return {
        "start": "2026-08-01",
        "end": "2026-08-02",
        "timezone": "Asia/Shanghai",
        "page_limit": 10,
        "max_messages": 500,
        "max_images": 100,
    }


async def test_chat_digest_plugin_binds_identity_and_verifies_live_resource() -> None:
    service = FakeDigestService()
    plugin = FeishuChatDigestPlugin(service, Identity.BOT)
    tool = plugin.tools[0]
    values = arguments()

    tool.validate(values)
    observation = await tool.execute(values, context())
    verification = await tool.verify(values, observation, context())

    assert plugin.manifest.plugin_id == "feishu-chat-digest"
    assert tool.definition.tool_id == "feishu.chat.digest.publish"
    assert service.requests[0].identity is Identity.BOT
    assert service.requests[0].chat_id == "oc_group"
    assert service.requests[0].chat_name is None
    assert verification.state is VerificationState.VERIFIED
    assert verification.resource_refs == ("https://example.feishu.cn/docx/group_digest",)
    assert tool.checkpoint_safe_observation is False


async def test_chat_digest_target_is_bound_to_trusted_group() -> None:
    tool = PublishChatDigestTool(FakeDigestService(), Identity.USER)
    first = await tool.resolve_write_target(arguments(), context())
    second = await tool.resolve_delegation_target({"resource": "current_chat"}, context())

    assert first == second
    with pytest.raises(PermissionError, match="originating trusted group"):
        await tool.resolve_delegation_target({"resource": "oc_other"}, context())


async def test_chat_digest_rejects_missing_group_before_service_access() -> None:
    service = FakeDigestService()
    tool = PublishChatDigestTool(service, Identity.USER)
    unbound = ToolContext("run", "/root", "tenant", "app", "user", "session", "user", 1)

    with pytest.raises(PermissionError, match="trusted originating group"):
        await tool.execute(arguments(), unbound)

    assert service.requests == []


async def test_chat_digest_verification_requires_resource_reference() -> None:
    tool = PublishChatDigestTool(FakeDigestService(), Identity.USER)
    verification = await tool.verify(arguments(), {"verification": {"status": "passed"}}, context())

    assert verification.state is VerificationState.FAILED


async def test_chat_digest_verification_rejects_bare_document_token() -> None:
    tool = PublishChatDigestTool(FakeDigestService(), Identity.USER)
    verification = await tool.verify(
        arguments(),
        {
            "verification": {"status": "passed"},
            "resource": {"document_id": "docx_token_only"},
        },
        context(),
    )

    assert verification.state is VerificationState.FAILED
    assert verification.resource_refs == ()
