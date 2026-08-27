from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import time
from collections.abc import Sequence
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .adapters.lark_cli import SUPPORTED_LARK_CLI_VERSION, safe_tool_call_error
from .bootstrap.config import GatewayConfig, resolve_data_dir
from .bootstrap.gateway import create_v3_gateway
from .bootstrap.process_control import GatewayProcessController, GatewayStatusFiles
from .capabilities.im.channel_adapter import ChannelPort, create_official_channel
from .interfaces.application import create_application
from .interfaces.mcp import run_stdio
from .runtime.resources import ResourceLoader
from .storage.crypto import MasterKey
from .storage.key_rotation import KeyRotationResult, KeyRotationService
from .storage.live_acceptance import LiveMultiGroupAcceptance
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


async def _gateway(config: GatewayConfig, channel: ChannelPort) -> int:
    gateway = create_v3_gateway(config, channel=channel)
    status_files = GatewayStatusFiles(config.data_dir)
    pid = os.getpid()
    started_at_ms = int(time.time() * 1000)
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        with suppress(NotImplementedError):
            loop.add_signal_handler(signum, shutdown.set)
    if os.environ.get("CODEX2LARK_DAEMON_CHILD") == "1":
        for _ in range(100):
            if status_files.read_pid() == pid:
                break
            await asyncio.sleep(0.01)
        else:
            raise RuntimeError("daemon parent did not publish the Gateway PID")
    else:
        status_files.publish("starting", pid=pid, started_at_ms=started_at_ms)
    health_monitor: asyncio.Task[None] | None = None

    async def publish_source_health() -> None:
        health = gateway.source_health()
        while True:
            status_files.publish(
                "ready" if health.ready else "degraded",
                pid=pid,
                started_at_ms=started_at_ms,
                source_state=health.state,
                provider_state=gateway.provider_state(),
                reconnect_attempts=health.reconnect_attempts,
            )
            health = await gateway.wait_source_health_change(health.version)

    try:
        await gateway.start()
        health_monitor = asyncio.create_task(
            publish_source_health(), name="codex2lark-source-health"
        )
        await shutdown.wait()
    finally:
        if health_monitor is not None:
            health_monitor.cancel()
            with suppress(asyncio.CancelledError):
                await health_monitor
        health = gateway.source_health()
        status_files.publish(
            "stopping",
            pid=pid,
            started_at_ms=started_at_ms,
            source_state=health.state,
            provider_state=gateway.provider_state(),
            reconnect_attempts=health.reconnect_attempts,
        )
        try:
            await gateway.stop()
        finally:
            status_files.clear_if_owner(pid)
    return 0


def _run_gateway() -> int:
    config = GatewayConfig.from_environment()
    channel = create_official_channel(
        app_id=config.feishu_app_id,
        app_secret=config.feishu_app_secret,
    )
    return asyncio.run(_gateway(config, channel))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex2lark")
    parser.add_argument("--version", action="version", version=__version__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("mcp", help="run the stdio MCP server")
    gateway = subcommands.add_parser("gateway", help="control the standalone Feishu Gateway")
    gateway.add_argument(
        "gateway_action",
        nargs="?",
        choices=("run", "start", "status", "stop"),
        default="run",
    )
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
    purge_tenant = storage_commands.add_parser(
        "purge-tenant", help="purge all local business state for one exact tenant"
    )
    purge_tenant.add_argument("--tenant-key", required=True)
    purge_tenant.add_argument("--yes", action="store_true")
    purge_all = storage_commands.add_parser(
        "purge-all", help="purge every local business row and encrypted blob"
    )
    purge_all.add_argument("--yes", action="store_true")
    rotate = storage_commands.add_parser(
        "rotate-key", help="rewrap all encrypted state with a new master key"
    )
    rotate.add_argument("--new-key-id", required=True)
    rotate.add_argument("--new-key-base64", required=True)
    rotate.add_argument("--yes", action="store_true")
    acceptance = subcommands.add_parser("acceptance", help="run explicit release gates")
    acceptance_commands = acceptance.add_subparsers(dest="acceptance_command", required=True)
    live = acceptance_commands.add_parser(
        "live-multigroup", help="observe the opt-in live multi-group release gate"
    )
    live.add_argument("--chat-id", action="append", required=True)
    live.add_argument("--since-ms", type=int, required=True)
    live.add_argument("--timeout-seconds", type=float, default=300.0)
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
        elif arguments.storage_command == "purge-tenant":
            if not arguments.yes:
                raise ValueError("storage purge-tenant requires explicit --yes confirmation")
            result = StorageMaintenance(resolve_data_dir()).purge_tenant(
                tenant_key=arguments.tenant_key
            )
        elif arguments.storage_command == "purge-all":
            if not arguments.yes:
                raise ValueError("storage purge-all requires explicit --yes confirmation")
            result = StorageMaintenance(resolve_data_dir()).purge_all()
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


def _acceptance(arguments: argparse.Namespace) -> int:
    try:
        if arguments.acceptance_command != "live-multigroup":
            return 2
        observer = LiveMultiGroupAcceptance(resolve_data_dir())
        result = observer.wait(
            tuple(arguments.chat_id),
            since_ms=arguments.since_ms,
            timeout_seconds=arguments.timeout_seconds,
        )
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        return 1
    print(observer.as_json(result))
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
            controller = GatewayProcessController(resolve_data_dir())
            if arguments.gateway_action == "start":
                print(GatewayStatusFiles.as_json(controller.start()))
                return 0
            if arguments.gateway_action == "status":
                status = controller.files.read()
                print(GatewayStatusFiles.as_json(status))
                return 0 if status.ok else 1
            if arguments.gateway_action == "stop":
                print(GatewayStatusFiles.as_json(controller.stop()))
                return 0
            return _run_gateway()
        except (RuntimeError, TimeoutError, ValueError) as exc:
            logging.error("Gateway configuration is invalid: %s", exc)
            return 2
        except KeyboardInterrupt:
            return 130
    if arguments.command == "storage":
        return _storage(arguments)
    if arguments.command == "acceptance":
        return _acceptance(arguments)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
