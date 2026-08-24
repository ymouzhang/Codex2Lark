from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .adapters.lark_cli import SUPPORTED_LARK_CLI_VERSION, safe_tool_call_error
from .bootstrap.config import GatewayConfig, resolve_data_dir
from .bootstrap.gateway import create_v3_gateway
from .interfaces.application import create_application
from .interfaces.mcp import run_stdio
from .runtime.resources import ResourceLoader
from .storage.crypto import MasterKey
from .storage.key_rotation import KeyRotationResult, KeyRotationService
from .storage.maintenance import (
    BackupResult,
    GarbageCollectionResult,
    PurgeResult,
    StorageMaintenance,
    StorageStatus,
)

_DOCTOR_DEADLINE_SECONDS = 20.0


async def _doctor() -> int:
    application = create_application()
    if not application.lark.available():
        print(
            json.dumps(
                {
                    "ok": False,
                    "checks": {
                        "lark_cli": "missing",
                        "mcp_server": "available",
                        "interactive_authoring": "unavailable",
                        "interactive_document_persistence": "disabled",
                    },
                    "next_action": "install and authenticate lark-cli",
                },
                ensure_ascii=False,
            )
        )
        return 1
    try:
        async with asyncio.timeout(_DOCTOR_DEADLINE_SECONDS):
            installed_version = await application.lark.version()
            if installed_version != SUPPORTED_LARK_CLI_VERSION:
                print(
                    json.dumps(
                        {
                            "ok": False,
                            "checks": {
                                "lark_cli": "available",
                                "lark_cli_version": {
                                    "installed": installed_version,
                                    "required": SUPPORTED_LARK_CLI_VERSION,
                                },
                                "interactive_authoring": "unavailable",
                                "interactive_document_persistence": "disabled",
                            },
                            "next_action": (
                                f"install the pinned CLI with "
                                f"npx @larksuite/cli@{SUPPORTED_LARK_CLI_VERSION} install"
                            ),
                        },
                        ensure_ascii=False,
                    )
                )
                return 1
            status = await application.lark.auth_status(verify=True)
    except TimeoutError:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": {
                        "category": "timeout",
                        "message": "interactive lark-cli diagnostic timed out",
                        "details": {"deadline_seconds": _DOCTOR_DEADLINE_SECONDS},
                    },
                    "next_action": (
                        "run `lark-cli auth status --json --verify` directly and check "
                        "network connectivity before retrying"
                    ),
                },
                ensure_ascii=False,
            )
        )
        return 1
    except Exception as exc:
        print(json.dumps(safe_tool_call_error(exc), ensure_ascii=False))
        return 1
    identities = status.data.get("identities")
    active_status = identities.get(status.identity) if isinstance(identities, dict) else None
    authentication_available = (
        status.identity not in (None, "none")
        and isinstance(active_status, dict)
        and active_status.get("available") is True
    )
    if not authentication_available:
        print(
            json.dumps(
                {
                    "ok": False,
                    "checks": {
                        "lark_cli": "available",
                        "lark_cli_version": installed_version,
                        "authentication": status.data,
                        "interactive_authoring": "unavailable",
                        "interactive_document_persistence": "disabled",
                    },
                    "next_action": "configure credentials or run lark-cli auth login --recommend",
                },
                ensure_ascii=False,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "version": __version__,
                "checks": {
                    "lark_cli": "available",
                    "lark_cli_version": installed_version,
                    "authentication": status.data,
                    "interactive_authoring": "ready",
                    "interactive_document_persistence": "disabled",
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


def _doctor_gateway() -> int:
    try:
        config = GatewayConfig.from_environment()
        loader = ResourceLoader.from_package("codex2lark.bundled_resources")
        group = loader.load(("group-agent-core",))
        worker = loader.load(("delegated-worker-core",))
        templates = ResourceLoader.load_im_templates("codex2lark.bundled_resources", "zh-CN")
        database_path = config.data_dir / "runtime.db"
        storage: object
        if database_path.is_file():
            status = StorageMaintenance(config.data_dir).status()
            storage = asdict(status)
            if not status.ok:
                raise RuntimeError(f"existing runtime storage is unhealthy: {status.integrity}")
        else:
            storage = "not_initialized"
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(
        json.dumps(
            {
                "ok": True,
                "version": __version__,
                "checks": {
                    "gateway_configuration": "valid",
                    "credentials": "configured",
                    "master_key": "valid_32_byte_key",
                    "agent_resources": {**group.versions, **worker.versions},
                    "im_templates": {
                        "bundle_id": templates.bundle_id,
                        "version": templates.version,
                    },
                    "storage": storage,
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


async def _gateway() -> int:
    gateway = create_v3_gateway(GatewayConfig.from_environment())
    await gateway.start()
    try:
        await asyncio.Event().wait()
    finally:
        await gateway.stop()
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex2lark")
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("mcp", help="run the stdio MCP server")
    subcommands.add_parser("gateway", help="run the standalone Feishu event gateway")
    doctor = subcommands.add_parser("doctor", help="check interactive or Gateway readiness")
    doctor.add_argument("--gateway", action="store_true")
    storage = subcommands.add_parser("storage", help="inspect and protect V3 runtime state")
    storage_commands = storage.add_subparsers(dest="storage_command", required=True)
    storage_commands.add_parser("status", help="check storage integrity and safe counts")
    backup = storage_commands.add_parser("backup", help="create an encrypted-state backup")
    backup.add_argument("archive", type=Path)
    verify = storage_commands.add_parser("verify-backup", help="verify a backup archive")
    verify.add_argument("archive", type=Path)
    restore = storage_commands.add_parser("restore", help="restore into a new data directory")
    restore.add_argument("archive", type=Path)
    restore.add_argument("--data-dir", required=True, type=Path)
    gc = storage_commands.add_parser("gc", help="delete explicitly expired runtime content")
    gc.add_argument("--batch-size", type=int, default=500)
    gc.add_argument("--yes", action="store_true")
    purge_message = storage_commands.add_parser(
        "purge-message", help="purge one exact local IM message and derived state"
    )
    purge_message.add_argument("--tenant-key", required=True)
    purge_message.add_argument("--app-id", required=True)
    purge_message.add_argument("--message-id", required=True)
    purge_message.add_argument("--yes", action="store_true")
    purge_chat = storage_commands.add_parser(
        "purge-chat", help="purge one exact local IM chat and derived state"
    )
    purge_chat.add_argument("--tenant-key", required=True)
    purge_chat.add_argument("--app-id", required=True)
    purge_chat.add_argument("--chat-id", required=True)
    purge_chat.add_argument("--yes", action="store_true")
    rotate = storage_commands.add_parser(
        "rotate-key", help="rewrap all encrypted state with a new master key"
    )
    rotate.add_argument("--new-key-id", required=True)
    rotate.add_argument("--new-key-base64", required=True)
    rotate.add_argument("--yes", action="store_true")
    return parser


def _storage(arguments: argparse.Namespace) -> int:
    try:
        result: (
            StorageStatus | BackupResult | GarbageCollectionResult | PurgeResult | KeyRotationResult
        )
        if arguments.storage_command == "status":
            result = StorageMaintenance(resolve_data_dir()).status()
        elif arguments.storage_command == "backup":
            result = StorageMaintenance(resolve_data_dir()).backup(arguments.archive)
        elif arguments.storage_command == "verify-backup":
            result = StorageMaintenance.verify_backup(arguments.archive)
        elif arguments.storage_command == "restore":
            result = StorageMaintenance.restore(arguments.archive, arguments.data_dir)
        elif arguments.storage_command == "gc":
            if not arguments.yes:
                raise ValueError("storage gc requires explicit --yes confirmation")
            result = StorageMaintenance(resolve_data_dir()).garbage_collect(
                batch_size=arguments.batch_size
            )
        elif arguments.storage_command == "purge-message":
            if not arguments.yes:
                raise ValueError("storage purge-message requires explicit --yes confirmation")
            result = StorageMaintenance(resolve_data_dir()).purge_message(
                tenant_key=arguments.tenant_key,
                app_id=arguments.app_id,
                message_id=arguments.message_id,
            )
        elif arguments.storage_command == "purge-chat":
            if not arguments.yes:
                raise ValueError("storage purge-chat requires explicit --yes confirmation")
            result = StorageMaintenance(resolve_data_dir()).purge_chat(
                tenant_key=arguments.tenant_key,
                app_id=arguments.app_id,
                chat_id=arguments.chat_id,
            )
        elif arguments.storage_command == "rotate-key":
            if not arguments.yes:
                raise ValueError("storage rotate-key requires explicit --yes confirmation")
            current = MasterKey.from_base64(
                key_id=os.environ.get("CODEX2LARK_MASTER_KEY_ID", ""),
                encoded_key=os.environ.get("CODEX2LARK_MASTER_KEY_BASE64", ""),
            )
            target = MasterKey.from_base64(
                key_id=arguments.new_key_id,
                encoded_key=arguments.new_key_base64,
            )
            result = KeyRotationService(resolve_data_dir()).rotate(current, target)
        else:
            return 2
    except (FileNotFoundError, FileExistsError, LookupError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(StorageMaintenance.as_json(result))
    return 0 if result.ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "mcp":
        run_stdio()
        return 0
    if arguments.command == "doctor":
        return _doctor_gateway() if arguments.gateway else asyncio.run(_doctor())
    if arguments.command == "gateway":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        try:
            return asyncio.run(_gateway())
        except ValueError as exc:
            logging.error("Gateway configuration is invalid: %s", exc)
            return 2
        except KeyboardInterrupt:
            return 130
    if arguments.command == "storage":
        return _storage(arguments)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
