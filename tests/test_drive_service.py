from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from codex2lark.adapters.lark_cli import LarkCliResult
from codex2lark.core.errors import AmbiguityError, NotFoundError
from codex2lark.core.models import Identity
from codex2lark.services.drive import DriveService


def search_result(*items: dict[str, object]) -> LarkCliResult:
    return LarkCliResult(data={"has_more": False, "results": list(items)})


def candidate(title: str, token: str, doc_type: str = "DOCX") -> dict[str, object]:
    return {
        "entity_type": "DOC",
        "title_highlighted": f"<h>{title}</h>",
        "result_meta": {
            "token": token,
            "url": f"https://example.feishu.cn/docx/{token}",
            "doc_types": doc_type,
            "update_time_iso": "2026-08-24T12:00:00+08:00",
        },
    }


def root_files(*items: dict[str, object]) -> LarkCliResult:
    return LarkCliResult(data={"files": list(items), "has_more": False})


def root_folder(name: str, token: str) -> dict[str, object]:
    return {
        "name": name,
        "token": token,
        "type": "folder",
        "url": f"https://example.feishu.cn/drive/folder/{token}",
    }


class QueueLark:
    def __init__(self, *responses: LarkCliResult) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, ...]] = []

    async def execute(self, args: Sequence[str], *, cwd: Path | None = None) -> LarkCliResult:
        self.calls.append(tuple(args))
        return self.responses.pop(0)


@pytest.mark.asyncio
async def test_managed_folder_is_created_from_live_absence() -> None:
    lark = QueueLark(
        root_files(),
        LarkCliResult(
            data={
                "folder_token": "fld_managed",
                "name": "Codex2Lark",
                "url": "https://example.feishu.cn/drive/folder/fld_managed",
            }
        ),
    )
    drive = DriveService(lark)  # type: ignore[arg-type]

    folder = await drive.ensure_managed_folder(Identity.USER)

    assert folder["token"] == "fld_managed"
    assert lark.calls[0][:3] == ("drive", "files", "list")
    assert lark.calls[1][:2] == ("drive", "+create-folder")
    assert "--folder-token" not in lark.calls[1]


@pytest.mark.asyncio
async def test_duplicate_managed_folders_are_ambiguous() -> None:
    lark = QueueLark(
        root_files(
            root_folder("Codex2Lark", "fld_one"),
            root_folder("Codex2Lark", "fld_two"),
        )
    )
    drive = DriveService(lark)  # type: ignore[arg-type]

    with pytest.raises(AmbiguityError):
        await drive.ensure_managed_folder(Identity.USER)


@pytest.mark.asyncio
async def test_document_search_prefers_managed_folder_exact_match() -> None:
    lark = QueueLark(
        root_files(root_folder("Codex2Lark", "fld_managed")),
        search_result(candidate("技术方案", "docx_managed")),
    )
    drive = DriveService(lark)  # type: ignore[arg-type]

    result = await drive.search_documents("技术方案", Identity.USER)

    assert result["scope"] == "managed_folder"
    assert result["matches"][0]["token"] == "docx_managed"
    assert "--folder-tokens" in lark.calls[1]
    assert len(lark.calls) == 2


@pytest.mark.asyncio
async def test_document_search_falls_back_to_drive_and_normalizes_highlights() -> None:
    title = "Codex2Lark: AI 原生飞书内容编排架构方案与详细实施计划"
    lark = QueueLark(
        root_files(),
        search_result(candidate(title, "docx_legacy")),
    )
    drive = DriveService(lark)  # type: ignore[arg-type]

    result = await drive.search_documents(title, Identity.USER)

    assert result["scope"] == "drive"
    assert result["matches"][0]["title"].startswith("Codex2Lark")
    query = lark.calls[1][lark.calls[1].index("--query") + 1]
    assert len(query) == 30


@pytest.mark.asyncio
async def test_document_resolution_never_guesses() -> None:
    missing = DriveService(QueueLark(root_files(), search_result()))  # type: ignore[arg-type]
    with pytest.raises(NotFoundError):
        await missing.resolve_document("不存在", Identity.USER)

    ambiguous = DriveService(
        QueueLark(
            root_files(),
            search_result(candidate("同名", "docx_one"), candidate("同名", "docx_two")),
        )
    )  # type: ignore[arg-type]
    with pytest.raises(AmbiguityError):
        await ambiguous.resolve_document("同名", Identity.USER)
