from __future__ import annotations

import json
from typing import Any

from ..adapters.lark_cli import LarkCli, LarkCliResult
from ..authoring.verifier import find_first_value
from ..core.errors import Codex2LarkError, ErrorCategory
from ..core.models import Identity


class ChatMembershipService:
    """Ensures bot-visible digest groups also contain the authenticated user."""

    def __init__(self, lark: LarkCli) -> None:
        self.lark = lark

    async def ensure_current_user(self, *, chat_id: str, chat_identity: Identity) -> dict[str, Any]:
        if chat_identity is not Identity.BOT:
            return {
                "status": "not_applicable",
                "reason": "group_accessed_as_user",
                "changed": False,
            }

        open_id = await self._current_user_open_id()
        members = await self._list_users(chat_id)
        if open_id in self._member_ids(members.data):
            return {
                "status": "already_member",
                "member": "current_authenticated_user",
                "changed": False,
                "verified_as": "bot",
            }

        pagination = members.meta.get("pagination")
        incomplete = (
            members.data.get("has_more") is True
            or (isinstance(pagination, dict) and pagination.get("complete") is False)
            or bool(members.data.get("truncations"))
        )
        if incomplete:
            raise Codex2LarkError(
                ErrorCategory.VALIDATION,
                "group member inspection was incomplete; the current user was not invited",
                details={"chat_id": chat_id},
            )

        result = await self.lark.execute(
            [
                "im",
                "chat.members",
                "create",
                "--params",
                json.dumps(
                    {
                        "chat_id": chat_id,
                        "member_id_type": "open_id",
                        "succeed_type": 2,
                    },
                    separators=(",", ":"),
                ),
                "--data",
                json.dumps({"id_list": [open_id]}, separators=(",", ":")),
                "--as",
                "bot",
                "--format",
                "json",
            ]
        )
        rejected = self._rejected_ids(result.data)
        if open_id in rejected:
            raise Codex2LarkError(
                ErrorCategory.PERMISSION,
                "the bot could not add the current authenticated user to the group",
                details={"chat_id": chat_id, "reason": rejected[open_id]},
            )

        verified = await self.lark.execute(
            [
                "im",
                "chats",
                "get",
                "--chat-id",
                chat_id,
                "--as",
                "user",
                "--format",
                "json",
            ]
        )
        name = find_first_value(verified.data, {"name"})
        if not isinstance(name, str) or not name:
            raise Codex2LarkError(
                ErrorCategory.VERIFICATION,
                "the group invitation completed but user access could not be verified",
                details={"chat_id": chat_id},
            )
        return {
            "status": "added",
            "member": "current_authenticated_user",
            "changed": True,
            "verified_as": "user",
        }

    async def _current_user_open_id(self) -> str:
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
                "a current authenticated Feishu user is required for bot-visible groups",
            )
        if not isinstance(bot, dict) or bot.get("available") is not True:
            raise Codex2LarkError(
                ErrorCategory.AUTHENTICATION,
                "a configured Feishu bot identity is required for bot-visible groups",
            )
        open_id = user.get("openId")
        if not isinstance(open_id, str) or not open_id:
            raise Codex2LarkError(
                ErrorCategory.AUTHENTICATION,
                "the authenticated Feishu user did not include an open ID",
            )
        return open_id

    async def _list_users(self, chat_id: str) -> LarkCliResult:
        return await self.lark.execute(
            [
                "im",
                "+chat-members-list",
                "--chat-id",
                chat_id,
                "--member-types",
                "user",
                "--member-id-type",
                "open_id",
                "--page-all",
                "--page-limit",
                "100",
                "--page-delay",
                "0",
                "--as",
                "bot",
                "--format",
                "json",
            ]
        )

    @staticmethod
    def _member_ids(data: dict[str, Any]) -> set[str]:
        users = data.get("users", [])
        if not isinstance(users, list):
            return set()
        return {
            member_id
            for user in users
            if isinstance(user, dict)
            for member_id in (user.get("member_id"),)
            if isinstance(member_id, str)
        }

    @staticmethod
    def _rejected_ids(data: dict[str, Any]) -> dict[str, str]:
        rejected: dict[str, str] = {}
        for key, reason in (
            ("invalid_id_list", "invalid_or_unavailable"),
            ("not_existed_id_list", "not_existed"),
            ("pending_approval_id_list", "pending_approval"),
        ):
            values = data.get(key, [])
            if isinstance(values, list):
                rejected.update({value: reason for value in values if isinstance(value, str)})
        return rejected
