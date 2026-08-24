from __future__ import annotations

import asyncio
import re
import unicodedata
from typing import Any

from .errors import AmbiguityError, NotFoundError, VerificationError
from .lark_cli import LarkCli
from .models import Identity
from .verifier import find_first_value

MANAGED_FOLDER_NAME = "Codex2Lark"
_MAX_SEARCH_PAGES = 3
_HIGHLIGHT_TAG = re.compile(r"</?h(?:b)?>", re.IGNORECASE)


def _plain_title(value: Any) -> str:
    return _HIGHLIGHT_TAG.sub("", value).strip() if isinstance(value, str) else ""


def _normalized_title(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


class DriveService:
    def __init__(self, lark: LarkCli, *, folder_name: str = MANAGED_FOLDER_NAME) -> None:
        if not folder_name.strip():
            raise ValueError("managed folder name cannot be empty")
        self.lark = lark
        self.folder_name = folder_name.strip()
        self._folder_lock = asyncio.Lock()

    async def ensure_managed_folder(self, identity: Identity) -> dict[str, Any]:
        async with self._folder_lock:
            existing = await self._find_root_folders(identity)
            if len(existing) > 1:
                raise AmbiguityError(
                    "more than one managed Drive folder has the configured name",
                    details={"folder_name": self.folder_name, "candidates": existing},
                )
            if existing:
                return existing[0]

            created = await self.lark.execute(
                [
                    "drive",
                    "+create-folder",
                    "--name",
                    self.folder_name,
                    "--as",
                    identity.value,
                    "--format",
                    "json",
                ]
            )
            token = find_first_value(created.data, {"folder_token", "token"})
            if not isinstance(token, str) or not token:
                raise VerificationError("managed Drive folder was created without a usable token")
            url = find_first_value(created.data, {"url"})
            return {
                "title": self.folder_name,
                "token": token,
                "url": url if isinstance(url, str) else None,
                "doc_type": "FOLDER",
            }

    async def find_managed_folder(self, identity: Identity) -> dict[str, Any] | None:
        matches = await self._find_root_folders(identity)
        if len(matches) > 1:
            raise AmbiguityError(
                "more than one managed Drive folder has the configured name",
                details={"folder_name": self.folder_name, "candidates": matches},
            )
        return matches[0] if matches else None

    async def _find_root_folders(self, identity: Identity) -> list[dict[str, Any]]:
        result = await self.lark.execute(
            [
                "drive",
                "files",
                "list",
                "--as",
                identity.value,
                "--format",
                "json",
            ]
        )
        raw_files = result.data.get("files", [])
        if not isinstance(raw_files, list):
            return []
        expected = _normalized_title(self.folder_name)
        matches: list[dict[str, Any]] = []
        for raw in raw_files:
            if not isinstance(raw, dict) or raw.get("type") != "folder":
                continue
            name = raw.get("name")
            token = raw.get("token")
            if not isinstance(name, str) or not isinstance(token, str):
                continue
            if _normalized_title(name) != expected:
                continue
            url = raw.get("url")
            matches.append(
                {
                    "title": name,
                    "token": token,
                    "url": url if isinstance(url, str) else None,
                    "doc_type": "FOLDER",
                }
            )
        return matches

    async def search_documents(self, title: str, identity: Identity) -> dict[str, Any]:
        folder = await self.find_managed_folder(identity)
        if folder is not None:
            managed = await self._find_exact(
                title=title,
                doc_type="docx",
                identity=identity,
                folder_token=folder["token"],
            )
            if managed:
                return {
                    "ok": True,
                    "query": title,
                    "scope": "managed_folder",
                    "managed_folder": folder,
                    "matches": managed,
                    "warnings": [],
                }

        matches = await self._find_exact(
            title=title,
            doc_type="docx",
            identity=identity,
        )
        return {
            "ok": True,
            "query": title,
            "scope": "drive",
            "managed_folder": folder,
            "matches": matches,
            "warnings": (
                ["no exact managed-folder match; searched the visible Drive"]
                if folder is not None
                else ["managed folder does not exist; searched the visible Drive"]
            ),
        }

    async def resolve_document(self, title: str, identity: Identity) -> dict[str, Any]:
        result = await self.search_documents(title, identity)
        matches: list[dict[str, Any]] = result["matches"]
        if not matches:
            raise NotFoundError(
                "no Feishu document matched the exact title",
                details={"title": title, "scope": result["scope"]},
            )
        if len(matches) > 1:
            raise AmbiguityError(
                "more than one Feishu document matched the exact title",
                details={
                    "title": title,
                    "scope": result["scope"],
                    "candidates": matches,
                },
            )
        return matches[0]

    async def _find_exact(
        self,
        *,
        title: str,
        doc_type: str,
        identity: Identity,
        folder_token: str | None = None,
    ) -> list[dict[str, Any]]:
        query = title[:30]
        page_token: str | None = None
        matches: dict[str, dict[str, Any]] = {}
        expected = _normalized_title(title)

        for _ in range(_MAX_SEARCH_PAGES):
            args = [
                "drive",
                "+search",
                "--query",
                query,
                "--only-title",
                "--doc-types",
                doc_type,
                "--page-size",
                "20",
                "--as",
                identity.value,
                "--format",
                "json",
            ]
            if folder_token is not None:
                args.extend(["--folder-tokens", folder_token])
            if page_token is not None:
                args.extend(["--page-token", page_token])
            result = await self.lark.execute(args)
            raw_results = result.data.get("results", [])
            if isinstance(raw_results, list):
                for raw in raw_results:
                    candidate = self._compact_candidate(raw)
                    if candidate is None:
                        continue
                    if _normalized_title(candidate["title"]) != expected:
                        continue
                    key = candidate.get("token") or candidate.get("url")
                    if isinstance(key, str):
                        matches[key] = candidate
            has_more = result.data.get("has_more") is True
            next_token = result.data.get("page_token")
            if not has_more or not isinstance(next_token, str) or not next_token:
                break
            page_token = next_token

        return list(matches.values())

    @staticmethod
    def _compact_candidate(raw: Any) -> dict[str, Any] | None:
        if not isinstance(raw, dict):
            return None
        meta = raw.get("result_meta")
        metadata = meta if isinstance(meta, dict) else {}
        title = _plain_title(raw.get("title") or raw.get("title_highlighted"))
        token = metadata.get("token") or raw.get("token")
        url = metadata.get("url") or raw.get("url")
        if not title or not isinstance(token, str):
            return None
        doc_type = metadata.get("doc_types") or raw.get("entity_type")
        updated_at = metadata.get("update_time_iso") or metadata.get("update_time")
        return {
            "title": title,
            "token": token,
            "url": url if isinstance(url, str) else None,
            "doc_type": str(doc_type) if doc_type is not None else None,
            "updated_at": updated_at,
        }
