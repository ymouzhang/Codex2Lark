# Installation and operation

## 1. Prerequisites

- Python 3.12+ and `uv`;
- Node.js with `npx` for the recommended lark-cli installer;
- a Feishu/Lark account allowed to create an Open Platform application;
- a local Codex host for the bundled stdio MCP server.

## 2. Install and authenticate lark-cli

Follow the official lark-cli flow:

```bash
npx @larksuite/cli@latest install
lark-cli config init
lark-cli auth login --recommend
lark-cli auth status
```

Authentication may open or return a browser URL. Credentials are owned by
lark-cli and its credential store; this project neither receives nor persists
them itself.

## 3. Prepare the Python runtime

From the plugin root:

```bash
uv sync --all-groups
uv run codex2lark doctor
```

`doctor` must report the MCP runtime available, lark-cli available,
authentication valid, and business-data persistence disabled.

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
- authentication error: rerun `lark-cli auth login --recommend` and inspect
  `lark-cli auth status`.
- permission error: grant the missing scope reported by lark-cli, then log in
  again.
- startup timeout: run `uv sync` once in the installed plugin directory or
  increase the plugin-scoped MCP startup timeout.
- verification error: the write reached Feishu but the live resource did not
  satisfy the requested invariants; inspect it before retrying.
