from __future__ import annotations

from typing import Any, Protocol

from pydantic import ValidationError

from codex2lark.core.models import (
    CreateDocumentRequest,
    DetailLevel,
    DocumentFormat,
    EditDocumentRequest,
    Identity,
    InspectDocumentRequest,
    ResourceRef,
    SearchDocumentsRequest,
)
from codex2lark.runtime.tools import SemanticTool, ToolContext
from codex2lark.runtime.types import (
    ToolDefinition,
    ToolEffect,
    VerificationRecord,
    VerificationState,
)


class DocumentService(Protocol):
    async def search(self, request: SearchDocumentsRequest) -> dict[str, Any]: ...

    async def inspect(self, request: InspectDocumentRequest) -> dict[str, Any]: ...

    async def create(self, request: CreateDocumentRequest) -> dict[str, Any]: ...

    async def edit(self, request: EditDocumentRequest) -> dict[str, Any]: ...


def _object(properties: dict[str, object]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _text(*, nullable: bool = False) -> dict[str, object]:
    return {"type": ["string", "null"] if nullable else "string"}


def _resource(value: str) -> ResourceRef:
    return ResourceRef(url=value) if value.startswith("https://") else ResourceRef(token=value)


class _DocumentTool:
    definition: ToolDefinition
    checkpoint_safe_observation = False

    def __init__(self, service: DocumentService, identity: Identity) -> None:
        self._service = service
        self._identity = identity

    def validate(self, arguments: dict[str, object]) -> None:
        properties = self.definition.input_schema["properties"]
        assert isinstance(properties, dict)
        if set(arguments) != set(properties):
            raise ValueError("tool arguments must exactly match the strict schema")
        try:
            self._request(arguments)
        except ValidationError as exc:
            raise ValueError(str(exc)) from exc

    async def execute(
        self, arguments: dict[str, object], context: ToolContext
    ) -> dict[str, object]:
        del context
        return await self._invoke(self._request(arguments))

    async def verify(
        self,
        arguments: dict[str, object],
        observation: dict[str, object],
        context: ToolContext,
    ) -> VerificationRecord:
        del arguments, context
        verification = observation.get("verification")
        status = verification.get("status") if isinstance(verification, dict) else None
        if self.definition.effect is ToolEffect.READ:
            state = VerificationState.NOT_REQUIRED
        else:
            state = (
                VerificationState.VERIFIED
                if status in {"passed", "upstream_confirmed"}
                else VerificationState.FAILED
            )
        return VerificationRecord(
            state,
            f"{self.definition.tool_id}.read_back",
            str(status or "live read completed"),
            self._resource_refs(observation),
        )

    def _request(self, arguments: dict[str, object]) -> object:
        raise NotImplementedError

    async def _invoke(self, request: object) -> dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _resource_refs(observation: dict[str, object]) -> tuple[str, ...]:
        resource = observation.get("resource")
        if not isinstance(resource, dict):
            return ()
        for field in ("url", "document_url", "token", "document_id"):
            value = resource.get(field)
            if isinstance(value, str) and value:
                return (value,)
        return ()


class SearchDocumentsTool(_DocumentTool):
    definition = ToolDefinition(
        "feishu.docs.search",
        1,
        "Find Feishu documents by exact title in the managed Codex2Lark folder.",
        _object({"title": _text()}),
        ToolEffect.READ,
    )

    def _request(self, arguments: dict[str, object]) -> SearchDocumentsRequest:
        return SearchDocumentsRequest.model_validate(
            {"title": arguments.get("title"), "identity": self._identity}
        )

    async def _invoke(self, request: object) -> dict[str, Any]:
        assert isinstance(request, SearchDocumentsRequest)
        return await self._service.search(request)


class InspectDocumentTool(_DocumentTool):
    definition = ToolDefinition(
        "feishu.docs.inspect",
        1,
        "Read one live Feishu document by HTTPS URL or token before editing it.",
        _object({"resource": _text()}),
        ToolEffect.READ,
    )

    def _request(self, arguments: dict[str, object]) -> InspectDocumentRequest:
        value = arguments.get("resource")
        if not isinstance(value, str):
            raise ValueError("resource must be text")
        return InspectDocumentRequest(
            resource=_resource(value),
            format=DocumentFormat.XML,
            detail=DetailLevel.FULL,
            identity=self._identity,
        )

    async def _invoke(self, request: object) -> dict[str, Any]:
        assert isinstance(request, InspectDocumentRequest)
        return await self._service.inspect(request)


class CreateDocumentTool(_DocumentTool):
    definition = ToolDefinition(
        "feishu.docs.create",
        1,
        (
            "Create a rich Feishu document from validated Feishu XML in the managed folder and "
            "read it back. Use headings, tables, callouts, code, and Mermaid whiteboards when "
            "useful."
        ),
        _object(
            {
                "title": _text(),
                "content_xml": _text(),
                "required_text": {"type": "array", "items": _text()},
            }
        ),
        ToolEffect.WRITE,
    )

    def _request(self, arguments: dict[str, object]) -> CreateDocumentRequest:
        return CreateDocumentRequest.model_validate(
            {
                "title": arguments.get("title"),
                "content": arguments.get("content_xml"),
                "format": DocumentFormat.XML,
                "identity": self._identity,
                "verification": {
                    "expected_title": arguments.get("title"),
                    "required_text": arguments.get("required_text"),
                },
            }
        )

    async def _invoke(self, request: object) -> dict[str, Any]:
        assert isinstance(request, CreateDocumentRequest)
        return await self._service.create(request)


class EditDocumentTool(_DocumentTool):
    definition = ToolDefinition(
        "feishu.docs.edit",
        1,
        (
            "Apply one bounded live Feishu document edit, read it back, and notify the operator. "
            "Exactly one of resource or document_title must be non-null."
        ),
        _object(
            {
                "resource": _text(nullable=True),
                "document_title": _text(nullable=True),
                "command": {
                    "type": "string",
                    "enum": [
                        "append",
                        "overwrite",
                        "str_replace",
                        "block_insert_after",
                        "block_replace",
                        "block_delete",
                    ],
                },
                "content_xml": _text(nullable=True),
                "pattern": _text(nullable=True),
                "block_id": _text(nullable=True),
                "change_summary": _text(),
                "required_text": {"type": "array", "items": _text()},
            }
        ),
        ToolEffect.WRITE,
    )

    def _request(self, arguments: dict[str, object]) -> EditDocumentRequest:
        resource = arguments.get("resource")
        return EditDocumentRequest.model_validate(
            {
                "resource": (
                    _resource(resource).model_dump()
                    if isinstance(resource, str) and resource
                    else None
                ),
                "document_title": arguments.get("document_title"),
                "operations": [
                    {
                        "command": arguments.get("command"),
                        "content": arguments.get("content_xml"),
                        "pattern": arguments.get("pattern"),
                        "block_id": arguments.get("block_id"),
                        "format": DocumentFormat.XML,
                    }
                ],
                "change_summary": arguments.get("change_summary"),
                "identity": self._identity,
                "verification": {"required_text": arguments.get("required_text")},
            }
        )

    async def _invoke(self, request: object) -> dict[str, Any]:
        assert isinstance(request, EditDocumentRequest)
        return await self._service.edit(request)


def document_tools(service: DocumentService, identity: Identity) -> list[SemanticTool]:
    return [
        SearchDocumentsTool(service, identity),
        InspectDocumentTool(service, identity),
        CreateDocumentTool(service, identity),
        EditDocumentTool(service, identity),
    ]
