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
    return ToolContext("run", "/root", "tenant", "app", "user", "session", "user", 1)


def arguments() -> dict[str, object]:
    return {
        "chat_id": "oc_group",
        "chat_name": None,
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
    assert verification.state is VerificationState.VERIFIED
    assert verification.resource_refs == ("https://example.feishu.cn/docx/group_digest",)
    assert tool.checkpoint_safe_observation is False


async def test_chat_digest_target_is_stable_and_schema_rejects_ambiguity() -> None:
    tool = PublishChatDigestTool(FakeDigestService(), Identity.USER)
    first = await tool.resolve_write_target(arguments(), context())
    equivalent = arguments()
    equivalent["chat_id"] = "  OC_GROUP  "
    second = await tool.resolve_write_target(equivalent, context())

    assert first == second
    invalid = arguments()
    invalid["chat_name"] = "Another target"
    with pytest.raises(ValueError, match="exactly one"):
        tool.validate(invalid)


async def test_chat_digest_verification_requires_resource_reference() -> None:
    tool = PublishChatDigestTool(FakeDigestService(), Identity.USER)
    verification = await tool.verify(arguments(), {"verification": {"status": "passed"}}, context())

    assert verification.state is VerificationState.FAILED
