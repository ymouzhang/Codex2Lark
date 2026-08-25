from __future__ import annotations

from dataclasses import dataclass

from ..adapters.lark_cli import LarkCli
from ..services.artifacts import ArtifactsService
from ..services.chat_digest import ChatDigestService
from ..services.chat_membership import ChatMembershipService
from ..services.docs import DocsService
from ..services.drive import DriveService
from ..services.notification import NotificationService


@dataclass(frozen=True, slots=True)
class Application:
    lark: LarkCli
    drive: DriveService
    notifier: NotificationService
    membership: ChatMembershipService
    docs: DocsService
    sheets: ArtifactsService
    base: ArtifactsService
    whiteboard: ArtifactsService
    chat_digest: ChatDigestService


def create_application(lark: LarkCli | None = None) -> Application:
    client = lark or LarkCli()
    drive = DriveService(client)
    notifier = NotificationService(client)
    membership = ChatMembershipService(client)
    docs = DocsService(client, drive, notifier)
    artifacts = ArtifactsService(client, docs, drive)
    return Application(
        lark=client,
        drive=drive,
        notifier=notifier,
        membership=membership,
        docs=docs,
        sheets=artifacts,
        base=artifacts,
        whiteboard=artifacts,
        chat_digest=ChatDigestService(client, docs, drive, notifier, membership),
    )
