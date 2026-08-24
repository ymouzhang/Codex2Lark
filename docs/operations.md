# Codex2Lark installation and configuration

This guide covers first-time installation, Feishu authorization, and Gateway
configuration. For routine startup and shutdown after installation, see
[Usage and shutdown](usage.md).

The `gateway` command is the V3 single-node service composition root. The MCP
command remains an independent interactive interface.

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
    "interactive_authoring": "ready",
    "interactive_document_persistence": "disabled"
  }
}
```

If `ok` is `false`, follow the returned `next_action`, fix the issue, and run the
command again.

The interactive diagnostic has a 20-second total deadline for its lark-cli
version and verified-authentication probes. If lark-cli or its upstream
authentication check does not answer in that interval, `doctor` exits non-zero
with a `timeout` category and recommends checking lark-cli directly. This short
diagnostic deadline does not reduce the separate execution timeout used by
normal authoring operations.

This default check covers Codex/MCP interactive authoring. It does not imply
that the independent V3 group Runtime is stateless. After exporting the Gateway
environment, validate its local configuration separately:

```bash
uv run codex2lark doctor --gateway
```

This check parses all required secrets without printing them, validates the
32-byte master key, model/profile values, absolute state path, bundled Agent
resources and IM templates, and—when `runtime.db` already exists—storage schema,
referenced blobs, and SQLite integrity. A missing database is reported as
`not_initialized` and is healthy before the first Gateway start. This is a
local preflight; the actual `gateway` startup remains the live Feishu long-
connection and bot-identity readiness check.

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

## 5. Configure and start the V3 Gateway

Skip this section if you only create and edit Feishu content from Codex.

Create a custom Feishu application and configure its bot, then publish these
events in the Feishu developer console:

- `im.message.receive_v1` for mention-driven work;
- `im.message.recalled_v1` for immediate source invalidation;
- `im.chat.member.bot.added_v1` for immediate group-membership automation;
- `im.chat.member.bot.deleted_v1` for immediate access revocation and local
  cleanup.

Grant the least permissions needed for enabled capabilities. The IM runtime
requires message read/history, reply-as-bot, chat metadata, and message-resource
read permissions. Group-member automation additionally requires:

1. Grant the bot these permissions:
   - `im:chat.members:bot_access`
   - `im:chat.members:read`
   - `im:chat.members:write_only`
2. Open **Events and callbacks** and add the **Bot added to group chat** event:
   `im.chat.member.bot.added_v1`, the **Bot removed from group chat** event:
   `im.chat.member.bot.deleted_v1`, and the **Message recalled** event:
   `im.message.recalled_v1`.
3. Create and publish a new application version. Saving the configuration alone
   does not activate it.
4. Confirm that the current lark-cli user is within the application's
   availability scope.

Provide runtime secrets through the service environment or an external secret
provider. They are never written into the database:

Generate the 32-byte encryption key once and retain it in the secret provider:

```bash
openssl rand -base64 32
```

```bash
export CODEX2LARK_FEISHU_APP_ID='cli_xxx'
export CODEX2LARK_FEISHU_APP_SECRET='...'
export OPENAI_API_KEY='...'
export CODEX2LARK_MODEL='gpt-5'
export CODEX2LARK_MASTER_KEY_ID='local-v1'
export CODEX2LARK_MASTER_KEY_BASE64='a-base64-encoded-32-byte-key'
export CODEX2LARK_AUTHORING_IDENTITY='user'
```

`CODEX2LARK_AUTHORING_IDENTITY` is `user` or `bot` and selects the trusted
lark-cli identity used by authoring tools. Group text cannot override it. The
selected lark-cli identity must be authenticated and have the document/Drive
permissions listed below.

`CODEX2LARK_DATA_DIR` optionally selects the state directory. Its default is
the platform user-state directory. The directory contains SQLite state and
encrypted attachment blobs; losing the master key makes encrypted state
unreadable. Do not rotate or delete it without the documented backup/key
procedure.

Start the Gateway:

```bash
uv run codex2lark gateway start
uv run codex2lark gateway status
```

A healthy status is content-safe and includes `"state":"ready"`. For foreground
debugging use:

```bash
uv run codex2lark gateway run
```

The Gateway needs outbound internet access but no public IP, Webhook, RabbitMQ,
or Redis. Admission, tasks, run checkpoints, and reply intents are durable.
Expired leases are recovered after restart. Press `Ctrl+C` for a draining stop;
the service stops event intake, completes its bounded drain, checkpoints SQLite,
and then exits.

The built-in single-node controller stores only a PID, timestamps, and lifecycle
state under the data directory; logs go to `gateway.log` and must remain
content-safe. `gateway stop` validates the live process command before signaling
it and waits up to the bounded shutdown deadline. A stale or mismatched PID is
reported and never signaled. Use this controller only for direct single-node
operation; systemd/Docker deployments continue to own their process lifecycle.

### Disk capacity settings

The Gateway blocks only new attachment downloads at hard storage pressure;
message admission, text-only work, replies, status, and cleanup remain usable.
Optional overrides are positive byte counts:

```bash
export CODEX2LARK_STORAGE_MAX_BYTES=$((10 * 1024 * 1024 * 1024))
export CODEX2LARK_STORAGE_MIN_FREE_BYTES=$((512 * 1024 * 1024))
export CODEX2LARK_MAX_ATTACHMENT_BYTES=$((20 * 1024 * 1024))
```

## 6. Runtime storage operations

Routine diagnostics need only the configured data directory and never print
message, attachment, prompt, or document content:

```bash
uv run codex2lark storage status
```

The JSON result reports SQLite integrity, schema version, database/blob byte
counts, filesystem free bytes, storage-pressure state, and task/outbox states. A
non-`ok` integrity result exits non-zero. Stop the Gateway before running
`storage gc` or a targeted purge.

Remove one exact message or chat and its local derived state:

```bash
uv run codex2lark storage purge-message \
  --tenant-key tenant_x --app-id cli_x --message-id om_x --yes

uv run codex2lark storage purge-chat \
  --tenant-key tenant_x --app-id cli_x --chat-id oc_x --yes

uv run codex2lark storage purge-tenant --tenant-key tenant_x --yes

uv run codex2lark storage purge-all --yes
```

Exact-target commands fail for an unknown target and print only counts and
reclaimed bytes. Tenant purge removes every local app/chat/run belonging to the
tenant. All purge removes all local business state while preserving migrations
and one content-free audit record. None of these commands deletes corresponding
upstream Feishu data.

Rotate the wrapping key only after creating and verifying a backup and while the
Gateway is stopped. Keep the current key environment configured for this
command:

```bash
uv run codex2lark storage rotate-key \
  --new-key-id local-v2 \
  --new-key-base64 'a-new-base64-encoded-32-byte-key' \
  --yes
```

On success, replace the service's `CODEX2LARK_MASTER_KEY_ID` and
`CODEX2LARK_MASTER_KEY_BASE64` with the new values before restarting. If the
command is interrupted, do not start the Gateway or delete either key; rerun the
same command until the recovery marker is cleared.

Create a portable encrypted-state backup only while the Gateway is stopped:

```bash
uv run codex2lark storage backup /absolute/path/codex2lark-backup.zip
uv run codex2lark storage verify-backup /absolute/path/codex2lark-backup.zip
```

The command uses SQLite's backup API, includes only blob files referenced by
the snapshot, records a SHA-256 manifest, and fails rather than overwriting an
existing output. A process lock makes the command fail if the Gateway still
owns the data directory. The archive contains encrypted state, not the external master
key. Back up `CODEX2LARK_MASTER_KEY_BASE64` separately in the secret provider;
without the matching key the restored ciphertext is intentionally unreadable.

Restore requires a new or empty target directory and a stopped Gateway:

```bash
uv run codex2lark storage restore \
  /absolute/path/codex2lark-backup.zip \
  --data-dir /absolute/path/new-codex2lark-state
```

Restore verifies every manifest hash and the SQLite integrity check before it
publishes the recovered files. It rejects unexpected archive paths and never
extracts the master key.

Run one bounded retention pass only while the Gateway is stopped:

```bash
uv run codex2lark storage gc --yes --batch-size 500
```

`gc` considers only rows whose explicit `expires_at_ms` (or raw-event
`payload_expires_at_ms`) is due at the command's current clock. It clears due
raw event payloads and deletes due messages, attachments/parser output,
artifacts, and idempotency records. It deletes an encrypted blob only after the
same transaction has removed the expiring references and a second query proves
that no retained attachment references it. One pass never processes more than
`--batch-size` rows per category. The JSON result contains counts and reclaimed
encrypted bytes, never deleted content. `--yes` is mandatory because local
recovery context may be removed; create and verify a backup first when needed.

## 7. Stop and uninstall

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
