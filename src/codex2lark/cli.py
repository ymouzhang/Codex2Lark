from __future__ import annotations

import argparse
import asyncio
import json
import logging
from collections.abc import Sequence

from . import __version__
from .adapters.lark_cli import SUPPORTED_LARK_CLI_VERSION, safe_tool_call_error
from .bootstrap.config import GatewayConfig
from .bootstrap.gateway import create_v3_gateway
from .interfaces.application import create_application
from .interfaces.mcp import run_stdio


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
                        "business_data_persistence": "disabled",
                    },
                    "next_action": "install and authenticate lark-cli",
                },
                ensure_ascii=False,
            )
        )
        return 1
    try:
        installed_version = await application.lark.version()
    except Exception as exc:
        print(json.dumps(safe_tool_call_error(exc), ensure_ascii=False))
        return 1
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
                        "business_data_persistence": "disabled",
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
    try:
        status = await application.lark.auth_status(verify=True)
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
                        "business_data_persistence": "disabled",
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
                    "business_data_persistence": "disabled",
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
    subcommands.add_parser("doctor", help="check lark-cli and authentication")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "mcp":
        run_stdio()
        return 0
    if arguments.command == "doctor":
        return asyncio.run(_doctor())
    if arguments.command == "gateway":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
        try:
            return asyncio.run(_gateway())
        except ValueError as exc:
            logging.error("Gateway configuration is invalid: %s", exc)
            return 2
        except KeyboardInterrupt:
            return 130
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
