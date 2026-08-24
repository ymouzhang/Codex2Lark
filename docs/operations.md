# Codex2Lark installation and configuration

This guide covers first-time installation, Feishu authorization, and Gateway
configuration. For routine startup and shutdown after installation, see
[Usage and shutdown](usage.md).

This guide covers the currently implemented V2 executable. The approved V3
single-node persistent multi-Agent service has separate design and delivery
contracts in [architecture.md](architecture.md) and [roadmap.md](roadmap.md); do
not infer that those future storage or Agent features are already available.

Run every command in this guide from the Codex2Lark repository root, except for
the lark-cli installation command.

## 1. Requirements

- Python 3.12 or newer;
- `uv`;
- Node.js and `npx`;
- Codex;
- a Feishu account that can create a custom enterprise application.

## 2. Install and sign in to lark-cli

Codex2Lark pins `@larksuite/cli@1.0.89`:

```bash
npx @larksuite/cli@1.0.89 install
lark-cli config init
lark-cli auth login --recommend
lark-cli auth status
```

Do not use `@latest`. lark-cli manages Feishu credentials; Codex2Lark does not
store them.

## 3. Install Python dependencies

From the repository root, run:

```bash
uv sync --all-groups
uv run codex2lark doctor
```

A healthy result includes:

```json
{
  "ok": true,
  "checks": {
    "lark_cli_version": "1.0.89",
    "business_data_persistence": "disabled"
  }
}
```

If `ok` is `false`, follow the returned `next_action`, fix the issue, and run the
command again.

## 4. Connect Codex

Choose one method. Do not use both at the same time.

### Use the source checkout

Run `codex mcp add` once as described in
[Usage and shutdown](usage.md#2-register-the-source-checkout-with-codex-once).
Codex then starts MCP automatically; do not keep
`uv run codex2lark mcp` running manually.

### Use the installed plugin

The plugin includes `.mcp.json`, so Codex starts MCP automatically. Restart
Codex after installing or updating the plugin, then inspect the tools with
`/mcp`. Do not add a manual MCP registration with the same name.

## 5. Configure and start the Gateway

Skip this section if you only create and edit Feishu content from Codex.

To run automation immediately after the bot joins a group, complete the
following configuration in the Feishu developer console:

1. Grant the bot these permissions:
   - `im:chat.members:bot_access`
   - `im:chat.members:read`
   - `im:chat.members:write_only`
2. Open **Events and callbacks** and add the **Bot added to group chat** event:
   `im.chat.member.bot.added_v1`.
3. Create and publish a new application version. Saving the configuration alone
   does not activate it.
4. Confirm that the current lark-cli user is within the application's
   availability scope.

First run a two-second connection probe:

```bash
lark-cli event consume im.chat.member.bot.added_v1 --as bot --timeout 2s
```

A healthy result includes:

```text
[event] ready event_key=im.chat.member.bot.added_v1
[source] feishu-websocket: connected
```

Stop the probe, then start the Gateway:

```bash
uv run codex2lark gateway
```

After `INFO event gateway ready` appears, add the bot to a test group. A bot
that is already in the group does not produce a new join event; remove it and
add it again.

The Gateway needs outbound internet access but does not need a public IP address
or domain. The default in-memory queue does not replay events received while the
Gateway is stopped or unfinished tasks left when it exits.

## 6. Stop and uninstall

### Stop foreground processes

- Manually started MCP: press `Ctrl+C` in its terminal.
- Gateway: press `Ctrl+C` in its terminal.
- MCP started automatically by Codex: Codex manages it; close or restart Codex
  to stop or restart the child process.

### Remove the source MCP registration

```bash
codex mcp remove codex2lark
```

### Uninstall the plugin

First inspect the plugin source:

```bash
codex plugin list --json
```

Then use the marketplace name shown in the list:

```bash
codex plugin remove codex2lark@MARKETPLACE
```

Removing the MCP registration or plugin does not remove Feishu authorization,
the repository, or any Feishu resources already created.

### Sign out of Feishu

Run this command only when you want to revoke the local lark-cli sign-in:

```bash
lark-cli auth logout
```

## 7. Feishu permissions

Different operations require different Feishu permissions. When a permission
error occurs, use the missing scope returned by lark-cli as the source of truth.

Common permissions include:

| Capability | Common permissions |
|---|---|
| Create and edit documents | Docs, Drive, `space:folder:create`, `search:docs:read` |
| Send a notification after an edit | `im:message:send_as_bot` |
| Read group messages and generate a digest | `im:chat:read`, `im:message:readonly`, and user message-history permissions |
| Invite the current user to the bot's group | `im:chat.members:read`, `im:chat.members:write_only` |

New Docs, Sheets, and Base resources are placed in the `Codex2Lark` folder at
the Feishu Drive root. The system creates the folder when needed and does not
store its folder token locally.

## 8. Troubleshooting

- `lark_cli: missing`: reinstall `@larksuite/cli@1.0.89` and check `PATH`.
- lark-cli version mismatch: rerun the pinned-version installation command; do
  not use `@latest`.
- No usable identity: run `lark-cli auth login --recommend`.
- MCP is registered but no tools appear: restart Codex or create a new task,
  then inspect `/mcp` again.
- The document title is not unique: provide the document URL or rename duplicate
  documents.
- Duplicate `Codex2Lark` folders: retain one folder with the exact name and retry.
- The bot cannot invite the user: check member permissions, application
  availability scope, and group invitation policy.
- The Gateway cannot start: confirm that the event was published with a new
  application version and rerun the connection probe.
- The edit succeeded but notification failed: check
  `im:message:send_as_bot`; do not repeat the edit merely to resend a notification.
