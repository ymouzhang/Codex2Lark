from __future__ import annotations

from typing import Any

from .compiler import block_exists, compile_document, count_exact_pattern, preflight_content
from .drive_service import DriveService
from .errors import AmbiguityError, ConflictError, VerificationError
from .lark_cli import LarkCli, LarkCliResult, safe_tool_call_error
from .models import (
    CreateDocumentRequest,
    DetailLevel,
    DocumentFormat,
    EditCommand,
    EditDocumentRequest,
    InspectDocumentRequest,
    PublishDocumentRequest,
    ResourceRef,
    SearchDocumentsRequest,
    VerifyDocumentRequest,
)
from .notification_service import NotificationService
from .runtime import EphemeralWorkspace
from .verifier import extract_content, extract_resource, extract_revision, verify_document


class DocsService:
    def __init__(
        self,
        lark: LarkCli,
        drive: DriveService | None = None,
        notifier: NotificationService | None = None,
    ) -> None:
        self.lark = lark
        self.drive = drive or DriveService(lark)
        self.notifier = notifier or NotificationService(lark)

    async def search(self, request: SearchDocumentsRequest) -> dict[str, Any]:
        return await self.drive.search_documents(request.title, request.identity)

    async def inspect(self, request: InspectDocumentRequest) -> dict[str, Any]:
        result = await self.lark.execute(
            [
                "docs",
                "+fetch",
                "--doc",
                request.resource.value,
                "--doc-format",
                request.format.value,
                "--detail",
                request.detail.value,
                "--as",
                request.identity.value,
                "--format",
                "json",
            ]
        )
        return {
            "ok": True,
            "resource": extract_resource(result.data),
            "data": result.data,
            "revision": extract_revision(result.data),
            "warnings": list(result.warnings),
        }

    async def create(self, request: CreateDocumentRequest) -> dict[str, Any]:
        preflight_content(request.content, request.format)
        managed_folder = await self.drive.ensure_managed_folder(request.identity)
        verification_policy = request.verification.model_copy(
            update={"expected_title": request.verification.expected_title or request.title}
        )
        with EphemeralWorkspace() as workspace:
            suffix = "xml" if request.format.value == "xml" else "md"
            content_path = workspace.write_text(f"document.{suffix}", request.content)
            args = [
                "docs",
                "+create",
                "--doc-format",
                request.format.value,
                "--title",
                request.title,
                "--content",
                workspace.relative_reference(content_path),
                "--as",
                request.identity.value,
                "--format",
                "json",
                "--parent-token",
                managed_folder["token"],
            ]
            created = await self.lark.execute(args, cwd=workspace.path)

        resource = extract_resource(created.data)
        reference = resource.get("url") or resource.get("document_id") or resource.get("token")
        if not isinstance(reference, str):
            raise VerificationError(
                "created document response did not contain a usable URL or token"
            )
        inspected = await self.inspect(
            InspectDocumentRequest(
                resource=(
                    ResourceRef(url=reference)
                    if reference.startswith("http")
                    else ResourceRef(token=reference)
                ),
                format=request.format,
                detail=DetailLevel.FULL,
                identity=request.identity,
            )
        )
        verification = verify_document(inspected["data"], verification_policy)
        if verification.status != "passed":
            raise VerificationError(
                "document was created but read-back verification failed",
                details={"resource": resource, "verification": verification.as_dict()},
            )
        warnings = [*created.warnings, *inspected.get("warnings", [])]
        if request.verification.fail_on_warning and warnings:
            raise VerificationError(
                "document was created but warnings are forbidden by the verification policy",
                details={"resource": resource, "warnings": warnings},
            )
        return {
            "ok": True,
            "resource": resource,
            "managed_folder": managed_folder,
            "verification": verification.as_dict(),
            "warnings": warnings,
        }

    async def publish(self, request: PublishDocumentRequest) -> dict[str, Any]:
        return await self.create(
            CreateDocumentRequest(
                title=request.document.title,
                format=DocumentFormat.XML,
                content=compile_document(request.document),
                identity=request.identity,
                verification=request.verification,
            )
        )

    async def edit(self, request: EditDocumentRequest) -> dict[str, Any]:
        resource = request.resource
        if resource is None:
            assert request.document_title is not None
            resolved = await self.drive.resolve_document(request.document_title, request.identity)
            resolved_url = resolved.get("url")
            resolved_token = resolved.get("token")
            if isinstance(resolved_url, str):
                resource = ResourceRef(url=resolved_url)
            elif isinstance(resolved_token, str):
                resource = ResourceRef(token=resolved_token)
            else:
                raise VerificationError("resolved document did not contain a usable URL or token")
        before = await self.inspect(
            InspectDocumentRequest(
                resource=resource,
                format=DocumentFormat.XML,
                detail=DetailLevel.FULL,
                identity=request.identity,
            )
        )
        current_revision = before["revision"]
        if request.expected_revision is not None and current_revision != request.expected_revision:
            raise ConflictError(
                "document revision changed before the edit",
                details={
                    "expected_revision": request.expected_revision,
                    "current_revision": current_revision,
                },
            )

        results: list[LarkCliResult] = []
        warnings: list[str] = []
        if request.expected_revision is not None:
            warnings.append(
                "revision enforcement is best-effort for shortcut operations; live state "
                "was checked immediately before writes"
            )

        for index, operation in enumerate(request.operations):
            snapshot = before
            if index > 0:
                snapshot = await self.inspect(
                    InspectDocumentRequest(
                        resource=resource,
                        format=DocumentFormat.XML,
                        detail=DetailLevel.FULL,
                        identity=request.identity,
                    )
                )
            live_content = extract_content(snapshot["data"])
            if operation.block_id is not None and not block_exists(
                live_content, operation.block_id
            ):
                raise ConflictError(
                    "target block no longer exists in the live document",
                    details={"block_id": operation.block_id, "operation_index": index},
                )
            if operation.command is EditCommand.STR_REPLACE:
                assert operation.pattern is not None
                matches = count_exact_pattern(live_content, operation.pattern, operation.block_id)
                if matches == 0:
                    raise ConflictError(
                        "exact replacement target was not found in the live document",
                        details={"operation_index": index},
                    )
                if matches > 1:
                    raise AmbiguityError(
                        "exact replacement target matched more than once",
                        details={"operation_index": index, "matches": matches},
                    )
            if operation.content is not None:
                preflight_content(operation.content, operation.format)
            with EphemeralWorkspace() as workspace:
                args = [
                    "docs",
                    "+update",
                    "--doc",
                    resource.value,
                    "--command",
                    operation.command.value,
                    "--doc-format",
                    operation.format.value,
                    "--as",
                    request.identity.value,
                    "--format",
                    "json",
                ]
                if operation.content is not None:
                    suffix = "xml" if operation.format.value == "xml" else "md"
                    path = workspace.write_text(f"operation-{index}.{suffix}", operation.content)
                    args.extend(["--content", workspace.relative_reference(path)])
                if operation.pattern is not None:
                    args.extend(["--pattern", operation.pattern])
                if operation.block_id is not None:
                    args.extend(["--block-id", operation.block_id])
                result = await self.lark.execute(args, cwd=workspace.path)
            results.append(result)
            warnings.extend(result.warnings)

        inspected = await self.inspect(
            InspectDocumentRequest(
                resource=resource,
                format=DocumentFormat.XML,
                detail=DetailLevel.FULL,
                identity=request.identity,
            )
        )
        verification = verify_document(inspected["data"], request.verification)
        if verification.status != "passed":
            raise VerificationError(
                "document edit completed but read-back verification failed",
                details={"verification": verification.as_dict()},
            )
        live_resource = extract_resource(inspected["data"])
        try:
            notification = await self.notifier.document_edited(
                resource=live_resource,
                change_summary=request.change_summary,
                revision=inspected["revision"],
                operations_applied=len(results),
            )
        except Exception as exc:
            notification = {
                "status": "failed",
                "error": safe_tool_call_error(exc)["error"],
            }
            warnings.append(
                "document edit was verified, but the completion notification failed; "
                "do not repeat the edit solely to resend the message"
            )
        return {
            "ok": True,
            "resource": live_resource,
            "revision": inspected["revision"],
            "operations_applied": len(results),
            "verification": verification.as_dict(),
            "notification": notification,
            "warnings": warnings,
        }

    async def verify(self, request: VerifyDocumentRequest) -> dict[str, Any]:
        inspected = await self.inspect(
            InspectDocumentRequest(
                resource=request.resource,
                format=DocumentFormat.XML,
                detail=DetailLevel.FULL,
                identity=request.identity,
            )
        )
        verification = verify_document(inspected["data"], request.policy)
        return {
            "ok": verification.status == "passed",
            "resource": extract_resource(inspected["data"]),
            "revision": inspected["revision"],
            "verification": verification.as_dict(),
            "warnings": inspected["warnings"],
        }
