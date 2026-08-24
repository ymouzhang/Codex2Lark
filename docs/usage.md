# Codex2Lark usage and shutdown

This guide explains how to use Codex2Lark from Codex after installation. For
first-time installation and Feishu permission setup, see
[Installation and configuration](operations.md).

The Gateway is the durable V3 runtime. MCP remains the Codex-facing interactive
surface and is not required for group-triggered Agent execution.

Run every command in this guide from the Codex2Lark repository root, except for
`/mcp`, which runs inside Codex.

## 1. Understand the two processes

| Process | Purpose | Started by | Must remain running? |
|---|---|---|---|
| `codex2lark mcp` | Allows Codex to create, query, and edit Feishu content | Codex automatically | No manual process required |
| `codex2lark gateway` | Runs the persistent Feishu event, Agent-task, and result-delivery loops | User or process manager | Yes, for group-triggered work |

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

After configuring the environment in
[Installation and configuration](operations.md#5-configure-and-start-the-v3-gateway),
start the Gateway for mention-driven group work:

```bash
uv run codex2lark gateway
```

The following log indicates that the long connection is ready:

```text
INFO V3 gateway ready
```

The Gateway is independent of Codex and MCP. Closing Codex does not stop it. In
production, run this command with systemd, Docker, or another process manager.

The Gateway uses a Feishu long connection and requires no public IP address,
Webhook, RabbitMQ, Redis, or external database. It persists recoverable state in
local SQLite and stores attachment bytes encrypted under the configured master
key.

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
INFO V3 gateway stopped
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

Confirm all required environment variables, the published Feishu event
subscriptions, application availability, and outbound connectivity. Startup
fails before accepting events if secrets, encryption key, database, model
profile, or bot identity cannot be validated.

For detailed Feishu console configuration, see
[Installation and configuration](operations.md#5-configure-and-start-the-gateway).
