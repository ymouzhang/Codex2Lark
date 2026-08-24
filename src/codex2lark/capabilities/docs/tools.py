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
from codex2lark.runtime.targets import logical_reservation
from codex2lark.runtime.tools import (
    SemanticTool,
    ToolContext,
    ToolReconciliation,
    WriteScopeTarget,
)
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

    async def reconcile(
        self, arguments: dict[str, object], context: ToolContext
    ) -> ToolReconciliation:
        del arguments, context
        return ToolReconciliation(
            {},
            VerificationRecord(
                VerificationState.UNCERTAIN,
                f"{self.definition.tool_id}.reconcile",
                "the live effect cannot be identified conclusively",
            ),
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

    async def resolve_delegation_target(
        self, declaration: dict[str, object], context: ToolContext
    ) -> WriteScopeTarget:
        del context
        resource = declaration.get("resource")
        if not isinstance(resource, str):
            raise ValueError("document create reservation requires the exact title")
        return logical_reservation("docx-create", resource)

    async def resolve_write_target(
        self, arguments: dict[str, object], context: ToolContext
    ) -> WriteScopeTarget:
        del context
        return logical_reservation("docx-create", self._request(arguments).title)

    async def reconcile(
        self, arguments: dict[str, object], context: ToolContext
    ) -> ToolReconciliation:
        request = self._request(arguments)
        found = await self._service.search(
            SearchDocumentsRequest(title=request.title, identity=self._identity)
        )
        if found.get("scope") != "managed_folder":
            return await super().reconcile(arguments, context)
        matches = found.get("matches")
        if not isinstance(matches, list) or len(matches) != 1:
            return await super().reconcile(arguments, context)
        candidate = matches[0]
        if not isinstance(candidate, dict):
            return await super().reconcile(arguments, context)
        reference = candidate.get("url") or candidate.get("token")
        if not isinstance(reference, str) or not reference:
            return await super().reconcile(arguments, context)
        inspected = await self._service.inspect(
            InspectDocumentRequest(
                resource=_resource(reference),
                format=DocumentFormat.XML,
                detail=DetailLevel.FULL,
                identity=self._identity,
            )
        )
        required = arguments.get("required_text")
        content = str(inspected.get("data", ""))
        if (
            not isinstance(required, list)
            or not required
            or not all(isinstance(item, str) and item in content for item in required)
        ):
            return await super().reconcile(arguments, context)
        observation = {
            **inspected,
            "resource": candidate,
            "verification": {"status": "passed", "reconciled": True},
        }
        return ToolReconciliation(
            observation,
            VerificationRecord(
                VerificationState.VERIFIED,
                f"{self.definition.tool_id}.reconcile",
                "existing live document matches the interrupted create intent",
                (reference,),
            ),
        )


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

    async def resolve_delegation_target(
        self, declaration: dict[str, object], context: ToolContext
    ) -> WriteScopeTarget:
        del context
        resource = declaration.get("resource")
        if not isinstance(resource, str) or not resource:
            raise ValueError("document delegation target requires resource")
        return await self._live_target(_resource(resource))

    async def resolve_write_target(
        self, arguments: dict[str, object], context: ToolContext
    ) -> WriteScopeTarget:
        del context
        request = self._request(arguments)
        resource = request.resource
        if resource is None:
            assert request.document_title is not None
            found = await self._service.search(
                SearchDocumentsRequest(title=request.document_title, identity=self._identity)
            )
            matches = found.get("matches")
            if (
                not isinstance(matches, list)
                or len(matches) != 1
                or not isinstance(matches[0], dict)
            ):
                raise ValueError("document title did not resolve to exactly one live target")
            reference = matches[0].get("url") or matches[0].get("token")
            if not isinstance(reference, str) or not reference:
                raise ValueError("resolved document has no usable reference")
            resource = _resource(reference)
        return await self._live_target(resource)

    async def _live_target(self, resource: ResourceRef) -> WriteScopeTarget:
        inspected = await self._service.inspect(
            InspectDocumentRequest(
                resource=resource,
                format=DocumentFormat.XML,
                detail=DetailLevel.FULL,
                identity=self._identity,
            )
        )
        live = inspected.get("resource")
        canonical: str | None = None
        if isinstance(live, dict):
            for field in ("document_id", "doc_token", "token"):
                value = live.get(field)
                if isinstance(value, str) and value:
                    canonical = value
                    break
        if canonical is None:
            canonical = resource.value.rstrip("/").rsplit("/", 1)[-1]
        revision = inspected.get("revision")
        return WriteScopeTarget(
            "docx",
            canonical,
            str(revision) if revision is not None else None,
        )

    async def reconcile(
        self, arguments: dict[str, object], context: ToolContext
    ) -> ToolReconciliation:
        request = self._request(arguments)
        resource = request.resource
        if resource is None and request.document_title is not None:
            found = await self._service.search(
                SearchDocumentsRequest(title=request.document_title, identity=self._identity)
            )
            matches = found.get("matches")
            if isinstance(matches, list) and len(matches) == 1 and isinstance(matches[0], dict):
                reference = matches[0].get("url") or matches[0].get("token")
                if isinstance(reference, str) and reference:
                    resource = _resource(reference)
        required = arguments.get("required_text")
        if resource is None or not isinstance(required, list) or not required:
            return await super().reconcile(arguments, context)
        inspected = await self._service.inspect(
            InspectDocumentRequest(
                resource=resource,
                format=DocumentFormat.XML,
                detail=DetailLevel.FULL,
                identity=self._identity,
            )
        )
        content = str(inspected.get("data", ""))
        if not all(isinstance(item, str) and item in content for item in required):
            return await super().reconcile(arguments, context)
        reference = resource.value
        return ToolReconciliation(
            {**inspected, "verification": {"status": "passed", "reconciled": True}},
            VerificationRecord(
                VerificationState.VERIFIED,
                f"{self.definition.tool_id}.reconcile",
                "the required live document state already exists",
                (reference,),
            ),
        )


def document_tools(service: DocumentService, identity: Identity) -> list[SemanticTool]:
    return [
        SearchDocumentsTool(service, identity),
        InspectDocumentTool(service, identity),
        CreateDocumentTool(service, identity),
        EditDocumentTool(service, identity),
    ]
