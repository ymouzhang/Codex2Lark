from __future__ import annotations

import hashlib
import re
from typing import Any

from ..adapters.lark_cli import LarkCli
from ..authoring.verifier import find_first_value
from ..core.errors import Codex2LarkError, ErrorCategory

_WHITESPACE = re.compile(r"\s+")
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]()#+.!|])")


def _single_line(value: str) -> str:
    return _WHITESPACE.sub(" ", value).strip()


def _markdown_text(value: str) -> str:
    safe = _single_line(value).replace("<", "\u2039").replace(">", "\u203a")
    return _MARKDOWN_SPECIAL.sub(r"\\\1", safe)


class NotificationService:
    def __init__(self, lark: LarkCli) -> None:
        self.lark = lark

    async def document_edited(
        self,
        *,
        resource: dict[str, Any],
        change_summary: str,
        revision: int | None,
        operations_applied: int,
    ) -> dict[str, Any]:
        status = await self.lark.auth_status(verify=False)
        identities = status.data.get("identities")
        if not isinstance(identities, dict):
            raise Codex2LarkError(
                ErrorCategory.AUTHENTICATION,
                "lark-cli authentication status did not include identities",
            )
        user = identities.get("user")
        bot = identities.get("bot")
        if not isinstance(user, dict) or user.get("available") is not True:
            raise Codex2LarkError(
                ErrorCategory.AUTHENTICATION,
                "a current authenticated Feishu user is required for edit notifications",
            )
        if not isinstance(bot, dict) or bot.get("available") is not True:
            raise Codex2LarkError(
                ErrorCategory.AUTHENTICATION,
                "a configured Feishu bot identity is required for edit notifications",
            )
        open_id = user.get("openId")
        if not isinstance(open_id, str) or not open_id:
            raise Codex2LarkError(
                ErrorCategory.AUTHENTICATION,
                "the authenticated Feishu user did not include an open ID",
            )

        title_value = find_first_value(resource, {"title", "name"})
        raw_title = _single_line(title_value) if isinstance(title_value, str) else "未命名文档"
        title = _markdown_text(raw_title)
        url_value = find_first_value(resource, {"url"})
        url = url_value if isinstance(url_value, str) and url_value.startswith("https://") else None
        token_value = find_first_value(resource, {"document_id", "token"})
        token = token_value if isinstance(token_value, str) else title
        raw_summary = _single_line(change_summary)
        summary = _markdown_text(raw_summary)
        document_label = f"[{title}]({url})" if url else title
        revision_label = str(revision) if revision is not None else "飞书未返回"
        message = (
            "#### Codex2Lark 文档更新完成\n\n"
            f"- 文档: {document_label}\n"
            f"- 修改: {summary}\n"
            f"- 操作数: {operations_applied}\n"
            "- 验证: 已通过实时回读\n"
            f"- 版本: {revision_label}"
        )
        content_value = find_first_value(resource, {"content", "raw_content", "markdown", "xml"})
        content = content_value if isinstance(content_value, str) else ""
        content_fingerprint = hashlib.sha256(content.encode()).hexdigest()
        digest = hashlib.sha256(
            f"{token}:{revision_label}:{raw_summary}:{content_fingerprint}".encode()
        ).hexdigest()[:32]
        result = await self.lark.execute(
            [
                "im",
                "+messages-send",
                "--user-id",
                open_id,
                "--markdown",
                message,
                "--idempotency-key",
                f"codex2lark-edit-{digest}",
                "--as",
                "bot",
                "--format",
                "json",
            ]
        )
        message_id = find_first_value(result.data, {"message_id"})
        return {
            "status": "sent",
            "message_id": message_id if isinstance(message_id, str) else None,
            "sent_as": "bot",
            "recipient": "current_authenticated_user",
        }
