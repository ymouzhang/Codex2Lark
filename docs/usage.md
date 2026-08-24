# Codex2Lark usage and shutdown

This guide explains how to use Codex2Lark from Codex after installation. For
first-time installation and Feishu permission setup, see
[Installation and configuration](operations.md).

This guide describes the currently implemented V2 commands. The durable V3
multi-Agent Runtime is an approved design, not a shipped command yet; see
[V3 architecture](architecture.md) and [delivery roadmap](roadmap.md).

Run every command in this guide from the Codex2Lark repository root, except for
`/mcp`, which runs inside Codex.

## 1. Understand the two processes

| Process | Purpose | Started by | Must remain running? |
|---|---|---|---|
| `codex2lark mcp` | Allows Codex to create, query, and edit Feishu content | Codex automatically | No manual process required |
| `codex2lark gateway` | Receives the currently implemented deterministic real-time events | User or process manager | Yes, when real-time events are enabled |

Routine Feishu document editing requires only MCP, not the Gateway.

## 2. Register the source checkout with Codex once

Skip this section when using the installed Codex2Lark plugin. The plugin already
includes its MCP configuration.

When using this repository directly, register it only once. First enter the
repository root:

```bash
cd /path/to/codex2lark
```

Confirm that the runtime is healthy:

```bash
uv run codex2lark doctor
```

Register MCP:

```bash
codex mcp add codex2lark \
  --env UV_CACHE_DIR=/tmp/codex2lark-uv-cache \
  -- uv run --project "$PWD" codex2lark mcp
```

Inspect the registration:

```bash
codex mcp get codex2lark
codex mcp list
```

`codex mcp add` stores the stdio startup command in the local Codex
configuration. Its argument form follows the
[Codex MCP command documentation](https://learn.chatgpt.com/docs/developer-commands#codex-mcp).

Restart Codex or create a new Codex task after registration. Do not separately
run `uv run codex2lark mcp`.

## 3. Daily use

### 3.1 Create or edit Feishu content

1. Start Codex.
2. Create a new task.
3. Enter `/mcp` and confirm that `codex2lark` appears.
4. Describe the desired Feishu work in natural language.

Codex automatically starts the MCP child process and selects the appropriate
tools. You do not need to invoke tool names manually.

Example: create a document

```text
Turn the solution we just discussed into a professional Feishu technical document.
Include the background, goals, architecture diagram, component-responsibility table,
and implementation plan. Read the document back after creation to verify it.
```

Example: edit a document

```text
Find the "Codex2Lark Architecture Proposal" document and add a disaster-recovery
plan after the deployment section. Do not change anything else. Read it back to
verify the edit, then notify me through the Feishu bot with a summary of changes.
```

Example: publish a group-chat digest

```text
Publish messages from the "Codex2Lark Project" group between 2026-08-20 and
2026-08-24 as a Feishu document. Order them by time and speaker, insert images,
and show only filenames for file attachments. Read the result back to verify it.
```

New Docs, Sheets, and Base resources are placed in the `Codex2Lark` Feishu Drive
folder by default.

### 3.2 Enable real-time Feishu events

Start the Gateway separately only for real-time behavior such as running an
action immediately after the bot is added to a group:

```bash
uv run codex2lark gateway
```

The following log indicates that the long connection is ready:

```text
INFO event gateway ready
```

The current Gateway is independent of Codex and MCP. Closing Codex does not stop
it. It does not yet run the V3 mention-driven, persistent multi-Agent workflow.
In production, run this command with systemd, Docker, or another process manager.

The Gateway uses a Feishu long connection and requires no public IP address,
Webhook, RabbitMQ, or database. Unprocessed in-memory tasks are not recovered
after the Gateway exits.

## 4. Stop the processes

### Stop MCP

Normally you do not stop MCP manually. Codex manages its lifecycle; close or
restart Codex to stop or restart the corresponding child process. Finishing one
task requires no additional action, but finishing a task is not a command that
forcibly stops MCP.

If you previously ran this command manually in a terminal:

```bash
uv run codex2lark mcp
```

press `Ctrl+C` in that terminal. Manual execution is only for protocol
debugging; Codex cannot connect to an already running stdio MCP process in a
different terminal.

### Stop the Gateway

When the Gateway runs in the foreground, press `Ctrl+C` in its terminal. The
following log confirms that it stopped:

```text
INFO event gateway stopped
```

When systemd or Docker manages it, use the corresponding service stop command.

### Prevent Codex from loading MCP again

Remove the source registration:

```bash
codex mcp remove codex2lark
```

Confirm its removal:

```bash
codex mcp list
```

Removing the registration does not remove the repository, Feishu authorization,
or any Feishu documents already created. For plugin-mode removal, see
[Installation and configuration](operations.md#6-stop-and-uninstall).

## 5. Troubleshooting

### The terminal appears idle after running `codex2lark mcp`

This is normal. The process is waiting for MCP stdio protocol input. Press
`Ctrl+C` to stop it, then register the startup command with Codex as described
in section 2.

### `codex2lark` does not appear in `/mcp`

Run these commands in order:

```bash
codex mcp get codex2lark
uv run codex2lark doctor
```

If both results are healthy, restart Codex or create a new task. Do not install
the plugin and manually register the same MCP at the same time.

### MCP fails to start

Confirm that `--project` in the registration command is the absolute repository
path, then run these commands in the repository:

```bash
uv sync --all-groups
uv run codex2lark doctor
```

### The Gateway fails to start

First confirm that the Feishu event and permissions were published, then run the
connection probe:

```bash
lark-cli event consume im.chat.member.bot.added_v1 --as bot --timeout 2s
```

A healthy result includes:

```text
[event] ready event_key=im.chat.member.bot.added_v1
[source] feishu-websocket: connected
```

For detailed Feishu console configuration, see
[Installation and configuration](operations.md#5-configure-and-start-the-gateway).
