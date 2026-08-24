from __future__ import annotations

import json
from typing import Any

from ..adapters.lark_cli import LarkCli
from ..authoring.compiler import whiteboard_xml
from ..authoring.verifier import extract_resource, find_first_value
from ..core.errors import VerificationError
from ..core.models import (
    CreateBaseRequest,
    CreateWorkbookRequest,
    EditDocumentRequest,
    EditOperation,
    UpsertBaseRecordsRequest,
    VerificationPolicy,
    WhiteboardRenderRequest,
    WriteSheetRequest,
)
from ..core.runtime import EphemeralWorkspace
from .docs import DocsService
from .drive import DriveService


class ArtifactsService:
    def __init__(self, lark: LarkCli, docs: DocsService, drive: DriveService) -> None:
        self.lark = lark
        self.docs = docs
        self.drive = drive

    async def render_whiteboard(self, request: WhiteboardRenderRequest) -> dict[str, Any]:
        if request.mode == "create":
            assert request.document is not None
            command = "block_insert_after" if request.anchor_block_id else "append"
            operation: dict[str, Any] = {
                "command": command,
                "content": whiteboard_xml(request.format, request.source),
                "format": "xml",
            }
            if request.anchor_block_id:
                operation["block_id"] = request.anchor_block_id
            return await self.docs.edit(
                EditDocumentRequest(
                    resource=request.document,
                    operations=[EditOperation.model_validate(operation)],
                    change_summary="新增或更新文档内的画板",
                    identity=request.identity,
                    verification=VerificationPolicy(min_blocks={"whiteboard": 1}),
                )
            )

        assert request.whiteboard_token is not None
        with EphemeralWorkspace(max_file_bytes=4_000_000) as workspace:
            suffix = {"mermaid": "mmd", "plantuml": "puml", "svg": "svg"}[request.format.value]
            path = workspace.write_text(f"diagram.{suffix}", request.source)
            args = [
                "whiteboard",
                "+update",
                "--whiteboard-token",
                request.whiteboard_token,
                "--input_format",
                request.format.value,
                "--source",
                workspace.relative_reference(path),
                "--as",
                request.identity.value,
                "--format",
                "json",
            ]
            if request.overwrite:
                args.append("--overwrite")
            result = await self.lark.execute(args, cwd=workspace.path)
        return {
            "ok": True,
            "resource": extract_resource(result.data),
            "verification": {"status": "upstream_confirmed", "checks": []},
            "warnings": list(result.warnings),
        }

    async def create_workbook(self, request: CreateWorkbookRequest) -> dict[str, Any]:
        managed_folder = await self.drive.ensure_managed_folder(request.identity)
        sheets = [sheet.model_dump(mode="json") for sheet in request.sheets]
        with EphemeralWorkspace() as workspace:
            sheets_path = workspace.write_text(
                "sheets.json", json.dumps(sheets, ensure_ascii=False, separators=(",", ":"))
            )
            args = [
                "sheets",
                "+workbook-create",
                "--title",
                request.title,
                "--sheets",
                workspace.relative_reference(sheets_path),
                "--as",
                request.identity.value,
                "--format",
                "json",
                "--folder-token",
                managed_folder["token"],
            ]
            if request.styles:
                styles_path = workspace.write_text(
                    "styles.json",
                    json.dumps(request.styles, ensure_ascii=False, separators=(",", ":")),
                )
                args.extend(["--styles", workspace.relative_reference(styles_path)])
            result = await self.lark.execute(args, cwd=workspace.path)
        return {
            "ok": True,
            "resource": extract_resource(result.data),
            "managed_folder": managed_folder,
            "verification": {"status": "upstream_confirmed", "checks": []},
            "warnings": list(result.warnings),
        }

    async def write_sheet(self, request: WriteSheetRequest) -> dict[str, Any]:
        with EphemeralWorkspace() as workspace:
            cells_path = workspace.write_text(
                "cells.json",
                json.dumps(request.cells, ensure_ascii=False, separators=(",", ":")),
            )
            result = await self.lark.execute(
                [
                    "sheets",
                    "+cells-set",
                    "--spreadsheet-token",
                    request.spreadsheet_token,
                    "--sheet-id",
                    request.sheet_id,
                    "--range",
                    request.range,
                    "--cells",
                    workspace.relative_reference(cells_path),
                    f"--allow-overwrite={str(request.allow_overwrite).lower()}",
                    "--as",
                    request.identity.value,
                    "--format",
                    "json",
                ],
                cwd=workspace.path,
            )
        read_back = await self.lark.execute(
            [
                "sheets",
                "+cells-get",
                "--spreadsheet-token",
                request.spreadsheet_token,
                "--sheet-id",
                request.sheet_id,
                "--range",
                request.range,
                "--include",
                "value,formula,style",
                "--as",
                request.identity.value,
                "--format",
                "json",
            ]
        )
        content = json.dumps(read_back.data, ensure_ascii=False)
        formula_errors = [
            marker
            for marker in ("#VALUE!", "#NAME?", "#REF!", "#N/A", "#DIV/0!", "#NUM!")
            if marker in content
        ]
        if formula_errors:
            raise VerificationError(
                "Sheet write returned formula errors during read-back",
                details={"formula_errors": formula_errors, "range": request.range},
            )
        return {
            "ok": True,
            "resource": {
                "spreadsheet_token": request.spreadsheet_token,
                "sheet_id": request.sheet_id,
                "range": request.range,
            },
            "verification": {"status": "passed", "checks": [{"formula_errors": []}]},
            "warnings": list(result.warnings) + list(read_back.warnings),
        }

    async def create_base(self, request: CreateBaseRequest) -> dict[str, Any]:
        managed_folder = await self.drive.ensure_managed_folder(request.identity)
        args = [
            "base",
            "+base-create",
            "--name",
            request.name,
            "--as",
            request.identity.value,
            "--format",
            "json",
            "--folder-token",
            managed_folder["token"],
        ]
        created = await self.lark.execute(args)
        base_token = find_first_value(created.data, {"app_token", "base_token"})
        if not isinstance(base_token, str):
            raise VerificationError("created Base response did not contain a base token")
        tables: list[dict[str, Any]] = []
        for table in request.tables:
            table_result = await self.lark.execute(
                [
                    "base",
                    "+table-create",
                    "--base-token",
                    base_token,
                    "--name",
                    table.name,
                    "--as",
                    request.identity.value,
                    "--format",
                    "json",
                ]
            )
            tables.append(extract_resource(table_result.data))
        return {
            "ok": True,
            "resource": {"base_token": base_token, "tables": tables},
            "managed_folder": managed_folder,
            "verification": {"status": "upstream_confirmed", "checks": []},
            "warnings": list(created.warnings),
        }

    async def upsert_base_records(self, request: UpsertBaseRecordsRequest) -> dict[str, Any]:
        command = "+record-batch-create" if request.mode == "create" else "+record-batch-update"
        with EphemeralWorkspace() as workspace:
            records_path = workspace.write_text(
                "records.json",
                json.dumps(request.records, ensure_ascii=False, separators=(",", ":")),
            )
            result = await self.lark.execute(
                [
                    "base",
                    command,
                    "--base-token",
                    request.base_token,
                    "--table-id",
                    request.table_id,
                    "--records",
                    workspace.relative_reference(records_path),
                    "--as",
                    request.identity.value,
                    "--format",
                    "json",
                ],
                cwd=workspace.path,
            )
        return {
            "ok": True,
            "resource": extract_resource(result.data),
            "verification": {"status": "upstream_confirmed", "checks": []},
            "warnings": list(result.warnings),
        }
