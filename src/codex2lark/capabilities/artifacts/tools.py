from __future__ import annotations

import json
from typing import Any, Protocol

from pydantic import ValidationError

from codex2lark.core.models import (
    CreateBaseRequest,
    CreateWorkbookRequest,
    Identity,
    ResourceRef,
    UpsertBaseRecordsRequest,
    WhiteboardRenderRequest,
    WriteSheetRequest,
)
from codex2lark.runtime.tools import SemanticTool, ToolContext, ToolReconciliation
from codex2lark.runtime.types import (
    ToolDefinition,
    ToolEffect,
    VerificationRecord,
    VerificationState,
)


class ArtifactService(Protocol):
    async def render_whiteboard(self, request: WhiteboardRenderRequest) -> dict[str, Any]: ...

    async def create_workbook(self, request: CreateWorkbookRequest) -> dict[str, Any]: ...

    async def write_sheet(self, request: WriteSheetRequest) -> dict[str, Any]: ...

    async def create_base(self, request: CreateBaseRequest) -> dict[str, Any]: ...

    async def upsert_base_records(self, request: UpsertBaseRecordsRequest) -> dict[str, Any]: ...


def _object(properties: dict[str, object]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _text(*, nullable: bool = False) -> dict[str, object]:
    return {"type": ["string", "null"] if nullable else "string"}


def _json(value: object, *, expected: type[list[object]] | type[dict[str, object]]) -> object:
    if not isinstance(value, str):
        raise ValueError("JSON argument must be text")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("JSON argument is invalid") from exc
    if not isinstance(parsed, expected):
        raise ValueError(f"JSON argument must decode to {expected.__name__}")
    return parsed


def _resource(value: str) -> ResourceRef:
    return ResourceRef(url=value) if value.startswith("https://") else ResourceRef(token=value)


class _ArtifactTool:
    definition: ToolDefinition
    checkpoint_safe_observation = False

    def __init__(self, service: ArtifactService, identity: Identity) -> None:
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
        state = (
            VerificationState.VERIFIED
            if status in {"passed", "upstream_confirmed"}
            else VerificationState.FAILED
        )
        return VerificationRecord(
            state,
            f"{self.definition.tool_id}.read_back",
            str(status or "verification missing"),
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
                "the live artifact effect cannot be identified conclusively",
            ),
        )

    def _request(self, arguments: dict[str, object]) -> object:
        raise NotImplementedError

    async def _invoke(self, request: object) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def _resource_refs(cls, observation: dict[str, object]) -> tuple[str, ...]:
        found: list[str] = []

        def visit(value: object) -> None:
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in {
                        "url",
                        "token",
                        "whiteboard_token",
                        "spreadsheet_token",
                        "base_token",
                        "app_token",
                    } and isinstance(item, str):
                        found.append(item)
                    else:
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(observation.get("resource"))
        return tuple(dict.fromkeys(found))


class RenderWhiteboardTool(_ArtifactTool):
    definition = ToolDefinition(
        "feishu.whiteboard.render",
        1,
        "Create a document whiteboard or update one from Mermaid, PlantUML, or SVG.",
        _object(
            {
                "mode": {"type": "string", "enum": ["create", "update"]},
                "format": {"type": "string", "enum": ["mermaid", "plantuml", "svg"]},
                "source": _text(),
                "document": _text(nullable=True),
                "whiteboard_token": _text(nullable=True),
                "anchor_block_id": _text(nullable=True),
                "overwrite": {"type": "boolean"},
            }
        ),
        ToolEffect.WRITE,
    )

    def _request(self, arguments: dict[str, object]) -> WhiteboardRenderRequest:
        document = arguments.get("document")
        return WhiteboardRenderRequest.model_validate(
            {
                **arguments,
                "document": (
                    _resource(document).model_dump()
                    if isinstance(document, str) and document
                    else None
                ),
                "identity": self._identity,
            }
        )

    async def _invoke(self, request: object) -> dict[str, Any]:
        assert isinstance(request, WhiteboardRenderRequest)
        return await self._service.render_whiteboard(request)


class CreateWorkbookTool(_ArtifactTool):
    _sheet = _object(
        {
            "name": _text(),
            "columns": {"type": "array", "items": _text()},
            "data_json": _text(),
            "dtypes_json": _text(),
            "formats_json": _text(),
        }
    )
    definition = ToolDefinition(
        "feishu.sheets.create",
        1,
        "Create a typed Feishu workbook in the managed folder.",
        _object(
            {
                "title": _text(),
                "sheets": {"type": "array", "items": _sheet},
                "styles_json": _text(),
            }
        ),
        ToolEffect.WRITE,
    )

    def _request(self, arguments: dict[str, object]) -> CreateWorkbookRequest:
        raw_sheets = arguments.get("sheets")
        if not isinstance(raw_sheets, list):
            raise ValueError("sheets must be an array")
        sheets: list[dict[str, object]] = []
        for raw in raw_sheets:
            if not isinstance(raw, dict):
                raise ValueError("each sheet must be an object")
            sheets.append(
                {
                    "name": raw.get("name"),
                    "columns": raw.get("columns"),
                    "data": _json(raw.get("data_json"), expected=list),
                    "dtypes": _json(raw.get("dtypes_json"), expected=dict),
                    "formats": _json(raw.get("formats_json"), expected=dict),
                }
            )
        return CreateWorkbookRequest.model_validate(
            {
                "title": arguments.get("title"),
                "sheets": sheets,
                "styles": _json(arguments.get("styles_json"), expected=list),
                "identity": self._identity,
            }
        )

    async def _invoke(self, request: object) -> dict[str, Any]:
        assert isinstance(request, CreateWorkbookRequest)
        return await self._service.create_workbook(request)


class WriteSheetTool(_ArtifactTool):
    definition = ToolDefinition(
        "feishu.sheets.write",
        1,
        "Write a bounded Sheet range and read values and formulas back for verification.",
        _object(
            {
                "spreadsheet_token": _text(),
                "sheet_id": _text(),
                "range": _text(),
                "cells_json": _text(),
                "allow_overwrite": {"type": "boolean"},
            }
        ),
        ToolEffect.WRITE,
    )

    def _request(self, arguments: dict[str, object]) -> WriteSheetRequest:
        return WriteSheetRequest.model_validate(
            {
                **arguments,
                "cells": _json(arguments.get("cells_json"), expected=list),
                "identity": self._identity,
            }
        )

    async def _invoke(self, request: object) -> dict[str, Any]:
        assert isinstance(request, WriteSheetRequest)
        return await self._service.write_sheet(request)


class CreateBaseTool(_ArtifactTool):
    definition = ToolDefinition(
        "feishu.base.create",
        1,
        "Create a Feishu Base and named tables in the managed folder.",
        _object(
            {
                "name": _text(),
                "table_names": {"type": "array", "items": _text()},
            }
        ),
        ToolEffect.WRITE,
    )

    def _request(self, arguments: dict[str, object]) -> CreateBaseRequest:
        names = arguments.get("table_names")
        if not isinstance(names, list):
            raise ValueError("table_names must be an array")
        return CreateBaseRequest.model_validate(
            {
                "name": arguments.get("name"),
                "tables": [{"name": name} for name in names],
                "identity": self._identity,
            }
        )

    async def _invoke(self, request: object) -> dict[str, Any]:
        assert isinstance(request, CreateBaseRequest)
        return await self._service.create_base(request)


class UpsertBaseRecordsTool(_ArtifactTool):
    definition = ToolDefinition(
        "feishu.base.upsert",
        1,
        "Create or update a bounded batch of records in one Feishu Base table.",
        _object(
            {
                "base_token": _text(),
                "table_id": _text(),
                "records_json": _text(),
                "mode": {"type": "string", "enum": ["create", "update"]},
            }
        ),
        ToolEffect.WRITE,
    )

    def _request(self, arguments: dict[str, object]) -> UpsertBaseRecordsRequest:
        return UpsertBaseRecordsRequest.model_validate(
            {
                **arguments,
                "records": _json(arguments.get("records_json"), expected=list),
                "identity": self._identity,
            }
        )

    async def _invoke(self, request: object) -> dict[str, Any]:
        assert isinstance(request, UpsertBaseRecordsRequest)
        return await self._service.upsert_base_records(request)


def artifact_tools(service: ArtifactService, identity: Identity) -> list[SemanticTool]:
    return [
        RenderWhiteboardTool(service, identity),
        CreateWorkbookTool(service, identity),
        WriteSheetTool(service, identity),
        CreateBaseTool(service, identity),
        UpsertBaseRecordsTool(service, identity),
    ]
