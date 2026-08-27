from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from codex2lark.adapters.lark_cli import LarkCliResult
from codex2lark.services.notification import NotificationService


class NotificationLark:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def auth_status(self, *, verify: bool = True) -> LarkCliResult:
        assert verify is False
        return LarkCliResult(
            data={
                "identity": "user",
                "identities": {
                    "user": {
                        "available": True,
                        "openId": "ou_current",
                    },
                    "bot": {"available": True},
                },
            }
        )

    async def execute(self, args: Sequence[str], *, cwd: Path | None = None) -> LarkCliResult:
        self.calls.append(tuple(args))
        return LarkCliResult(data={"message_id": "om_test"}, identity="bot")


@pytest.mark.asyncio
async def test_edit_notification_is_bot_dm_with_idempotency_key() -> None:
    lark = NotificationLark()
    notifier = NotificationService(lark)  # type: ignore[arg-type]

    result = await notifier.document_edited(
        resource={
            "document_id": "docx_test",
            "url": "https://example.feishu.cn/docx/docx_test",
            "content": "<title>技术方案</title><p>已更新</p>",
        },
        document_title='技术方案 <at user_id="all"></at>',
        change_summary="更新架构章节并保留实施计划",
        revision=8,
        operations_applied=2,
    )

    assert result == {
        "status": "sent",
        "message_id": "om_test",
        "sent_as": "bot",
        "recipient": "current_authenticated_user",
    }
    call = lark.calls[0]
    assert call[:2] == ("im", "+messages-send")
    assert call[call.index("--user-id") + 1] == "ou_current"
    assert call[call.index("--as") + 1] == "bot"
    message = call[call.index("--markdown") + 1]
    assert "技术方案" in message
    assert "更新架构章节" in message
    assert "<at" not in message
    key = call[call.index("--idempotency-key") + 1]
    assert key.startswith("codex2lark-edit-")
    assert len(key) <= 50


@pytest.mark.asyncio
async def test_edit_notification_requires_explicit_verified_title() -> None:
    notifier = NotificationService(NotificationLark())  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="verified document title"):
        await notifier.document_edited(
            resource={"url": "https://example.feishu.cn/docx/docx_test"},
            document_title="   ",
            change_summary="更新正文",
            revision=1,
            operations_applied=1,
        )
