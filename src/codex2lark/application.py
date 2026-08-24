from __future__ import annotations

from dataclasses import dataclass

from .artifacts_service import ArtifactsService
from .docs_service import DocsService
from .lark_cli import LarkCli


@dataclass(frozen=True, slots=True)
class Application:
    lark: LarkCli
    docs: DocsService
    artifacts: ArtifactsService


def create_application(lark: LarkCli | None = None) -> Application:
    client = lark or LarkCli()
    docs = DocsService(client)
    return Application(lark=client, docs=docs, artifacts=ArtifactsService(client, docs))
