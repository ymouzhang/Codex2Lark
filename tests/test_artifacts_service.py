from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from codex2lark.artifacts_service import ArtifactsService
from codex2lark.lark_cli import LarkCliResult
from codex2lark.models import CreateBaseRequest, CreateWorkbookRequest, SheetSpec


class ManagedDrive:
    async def ensure_managed_folder(self, identity: object) -> dict[str, object]:
        return {"title": "Codex2Lark", "token": "fld_managed"}


class ArtifactLark:
    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    async def execute(self, args: Sequence[str], *, cwd: Path | None = None) -> LarkCliResult:
        self.calls.append(tuple(args))
        if args[:2] == ["base", "+base-create"]:
            return LarkCliResult(data={"app_token": "bas_test"})
        return LarkCliResult(data={"spreadsheet_token": "sht_test"})


@pytest.mark.asyncio
async def test_workbook_creation_uses_managed_folder() -> None:
    lark = ArtifactLark()
    service = ArtifactsService(  # type: ignore[arg-type]
        lark,
        None,
        ManagedDrive(),
    )

    result = await service.create_workbook(
        CreateWorkbookRequest(
            title="数据表",
            sheets=[SheetSpec(name="明细", columns=["名称"], data=[["A"]])],
        )
    )

    call = lark.calls[0]
    assert call[call.index("--folder-token") + 1] == "fld_managed"
    assert result["managed_folder"]["token"] == "fld_managed"


@pytest.mark.asyncio
async def test_base_creation_uses_managed_folder() -> None:
    lark = ArtifactLark()
    service = ArtifactsService(  # type: ignore[arg-type]
        lark,
        None,
        ManagedDrive(),
    )

    result = await service.create_base(CreateBaseRequest(name="项目数据"))

    call = lark.calls[0]
    assert call[call.index("--folder-token") + 1] == "fld_managed"
    assert result["managed_folder"]["token"] == "fld_managed"
