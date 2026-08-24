from __future__ import annotations

from dataclasses import dataclass

from .artifacts_service import ArtifactsService
from .chat_digest_service import ChatDigestService
from .docs_service import DocsService
from .drive_service import DriveService
from .lark_cli import LarkCli
from .notification_service import NotificationService


@dataclass(frozen=True, slots=True)
class Application:
    lark: LarkCli
    drive: DriveService
    notifier: NotificationService
    docs: DocsService
    artifacts: ArtifactsService
    chat_digest: ChatDigestService


def create_application(lark: LarkCli | None = None) -> Application:
    client = lark or LarkCli()
    drive = DriveService(client)
    notifier = NotificationService(client)
    docs = DocsService(client, drive, notifier)
    return Application(
        lark=client,
        drive=drive,
        notifier=notifier,
        docs=docs,
        artifacts=ArtifactsService(client, docs, drive),
        chat_digest=ChatDigestService(client, docs, drive, notifier),
    )
