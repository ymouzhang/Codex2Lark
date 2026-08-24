# Installation and operation

## 1. Prerequisites

- Python 3.12+ and `uv`;
- Node.js with `npx` for the recommended lark-cli installer;
- a Feishu/Lark account allowed to create an Open Platform application;
- a local Codex host for the bundled stdio MCP server.

## 2. Install and authenticate lark-cli

Follow the official lark-cli flow:

```bash
npx @larksuite/cli@1.0.89 install
lark-cli config init
lark-cli auth login --recommend
lark-cli auth status
```

`@larksuite/cli` is an external Node.js runtime and is therefore not part of
Python's `uv.lock`. Codex2Lark pins version `1.0.89` in both this installation
command and its runtime compatibility check. Do not use `@latest`; after any
intentional upgrade, update the documented version, runtime constant, and tests
in the same change.

Authentication may open or return a browser URL. Credentials are owned by
lark-cli and its credential store; this project neither receives nor persists
them itself.

## 3. Prepare the Python runtime

From the plugin root:

```bash
uv sync --all-groups
uv run codex2lark doctor
```

`doctor` must report the MCP runtime available, the exact supported lark-cli
version (`1.0.89`), authentication valid, and business-data persistence
disabled. It invokes
`lark-cli auth status --json --verify` through the adapter's dedicated
authentication-status operation because this command returns a bare status
object rather than the normal `{ok, data}` command envelope. The check succeeds
only when the returned `identity` names an identity whose status is available;
an absent, `none`, unavailable, or malformed identity produces a failed doctor
result with the returned safe status details and a login/configuration action.

## 4. Run directly

```bash
uv run codex2lark mcp
```

The process speaks MCP over stdio. It does not print logs or document content to
stdout because that channel is reserved for protocol messages.

## 5. Use from Codex

The repository is a Codex plugin: `.codex-plugin/plugin.json` points to the
authoring Skill and bundled `.mcp.json`. The bundled server starts with `uv`,
uses the installed plugin root as its working directory, and exposes semantic
tools rather than an arbitrary shell surface.

The bundled configuration directs uv's package cache to
`/tmp/codex2lark-uv-cache`. This cache contains dependencies only, never
Feishu content or credentials, and avoids startup failures on hosts with a
read-only home cache.

For development without installing the plugin, register the same server as a
project-scoped MCP server and set its `cwd` to this repository. Codex supports
`command`, `args`, `env`, and `cwd` for local stdio servers. After enabling the
server, restart the host and inspect the available tools with `/mcp`.

## 6. First smoke test

Ask Codex to create a disposable Feishu document containing a heading, callout,
table, and Mermaid whiteboard. Confirm the response contains a live URL and a
passed read-back verification. Then ask it to make a surgical edit using that
URL and confirm unrelated text remains present.

Do not use a production document for the first test.

## 7. Troubleshooting

- `lark_cli: missing`: install lark-cli and ensure it is on the Codex host PATH.
- lark-cli version mismatch: install the pinned runtime with
  `npx @larksuite/cli@1.0.89 install`, then rerun `doctor`.
- authentication error: rerun `lark-cli auth login --recommend` and inspect
  `lark-cli auth status`.
- `return_code: 0` paired with `lark-cli operation failed` from an older
  Codex2Lark build: update Codex2Lark; older builds incorrectly parsed the bare
  authentication-status response as a failed normal command envelope.
- permission error: grant the missing scope reported by lark-cli, then log in
  again.
- startup timeout: run `uv sync` once in the installed plugin directory or
  increase the plugin-scoped MCP startup timeout.
- verification error: the write reached Feishu but the live resource did not
  satisfy the requested invariants; inspect it before retrying.
