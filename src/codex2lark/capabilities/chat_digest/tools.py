from __future__ import annotations

from typing import Any, Protocol

from pydantic import ValidationError

from codex2lark.core.models import ChatDigestRequest, Identity
from codex2lark.runtime.targets import logical_reservation
from codex2lark.runtime.tools import ToolContext, ToolReconciliation, WriteScopeTarget
from codex2lark.runtime.types import (
    ToolDefinition,
    ToolEffect,
    VerificationRecord,
    VerificationState,
)


class ChatDigestService(Protocol):
    async def publish(self, request: ChatDigestRequest) -> dict[str, Any]: ...


class PublishChatDigestTool:
    checkpoint_safe_observation = False
    definition = ToolDefinition(
        "feishu.chat.digest.publish",
        2,
        (
            "Create or refresh one verified chronological history document for the "
            "current trusted Feishu group. This is the required tool whenever the user "
            "asks to collect, archive, organize, or publish current-group messages; do "
            "not use generic document creation for that intent. "
            "Images may be embedded; ordinary files are represented by filename only."
        ),
        {
            "type": "object",
            "properties": {
                "start": {"type": "string"},
                "end": {"type": "string"},
                "timezone": {"type": "string"},
                "page_limit": {"type": "integer", "minimum": 1, "maximum": 100},
                "max_messages": {"type": "integer", "minimum": 1, "maximum": 2000},
                "max_images": {"type": "integer", "minimum": 0, "maximum": 500},
            },
            "required": [
                "start",
                "end",
                "timezone",
                "page_limit",
                "max_messages",
                "max_images",
            ],
            "additionalProperties": False,
        },
        ToolEffect.WRITE,
    )

    def __init__(self, service: ChatDigestService, identity: Identity) -> None:
        self._service = service
        self._identity = identity

    def validate(self, arguments: dict[str, object]) -> None:
        properties = self.definition.input_schema["properties"]
        assert isinstance(properties, dict)
        if set(arguments) != set(properties):
            raise ValueError("tool arguments must exactly match the strict schema")
        try:
            ChatDigestRequest.model_validate(
                {**arguments, "chat_id": "trusted-context-placeholder", "identity": self._identity}
            )
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc

    async def execute(
        self, arguments: dict[str, object], context: ToolContext
    ) -> dict[str, object]:
        return await self._service.publish(self._request(arguments, context))

    async def verify(
        self,
        arguments: dict[str, object],
        observation: dict[str, object],
        context: ToolContext,
    ) -> VerificationRecord:
        del arguments, context
        verification = observation.get("verification")
        status = verification.get("status") if isinstance(verification, dict) else None
        resource = observation.get("resource")
        refs: list[str] = []
        if isinstance(resource, dict):
            for field in ("url", "document_url"):
                value = resource.get(field)
                if isinstance(value, str) and value.startswith("https://"):
                    refs.append(value)
                    break
        state = (
            VerificationState.VERIFIED
            if status in {"passed", "upstream_confirmed"} and refs
            else VerificationState.FAILED
        )
        return VerificationRecord(
            state,
            "feishu.chat.digest.publish.read_back",
            str(status or "live digest verification missing"),
            tuple(refs),
        )

    async def reconcile(
        self, arguments: dict[str, object], context: ToolContext
    ) -> ToolReconciliation:
        del arguments, context
        return ToolReconciliation(
            {},
            VerificationRecord(
                VerificationState.UNCERTAIN,
                "feishu.chat.digest.publish.reconcile",
                "the interrupted digest must be resolved from live Feishu state",
            ),
        )

    async def resolve_write_target(
        self, arguments: dict[str, object], context: ToolContext
    ) -> WriteScopeTarget:
        request = self._request(arguments, context)
        assert request.chat_id is not None
        return logical_reservation("chat-digest", f"id:{request.chat_id}")

    async def resolve_delegation_target(
        self, declaration: dict[str, object], context: ToolContext
    ) -> WriteScopeTarget:
        resource = declaration.get("resource")
        if not isinstance(resource, str) or not resource:
            raise ValueError("chat digest reservation requires an exact chat target")
        chat_id = self._trusted_chat_id(context)
        if resource.strip() not in {"current_chat", chat_id}:
            raise PermissionError("chat digest target must match the originating trusted group")
        return logical_reservation("chat-digest", f"id:{chat_id}")

    def _request(self, arguments: dict[str, object], context: ToolContext) -> ChatDigestRequest:
        return ChatDigestRequest.model_validate(
            {
                **arguments,
                "chat_id": self._trusted_chat_id(context),
                "identity": self._identity,
            }
        )

    @staticmethod
    def _trusted_chat_id(context: ToolContext) -> str:
        if context.chat_id is None or not context.chat_id.strip():
            raise PermissionError("chat digest requires a trusted originating group binding")
        return context.chat_id.strip()


def chat_digest_tools(
    service: ChatDigestService, identity: Identity
) -> tuple[PublishChatDigestTool, ...]:
    return (PublishChatDigestTool(service, identity),)
