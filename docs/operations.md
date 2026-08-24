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

There are two alternative connection modes. Choose one; they are not sequential
setup steps.

### 5.1 Installed plugin mode

Use this mode after Codex2Lark has been installed as a Codex plugin. The plugin
contains three integration layers:

| File | Role |
|---|---|
| `.codex-plugin/plugin.json` | Identifies the plugin and points Codex to its packaged capabilities. |
| `skills/feishu-authoring/SKILL.md` | Tells the model when and how to compose safe Feishu authoring workflows. |
| `.mcp.json` | Tells Codex how to launch the local Codex2Lark stdio MCP server. |

This packaging follows the
[official Codex plugin guidance for bundled MCP servers](https://developers.openai.com/plugins/build/plugins#bundled-mcp-servers-and-lifecycle-hooks).

Codex reads these files and starts the MCP server with `uv` when the plugin is
enabled. The user does not manually keep `uv run codex2lark mcp` running. The
MCP server exposes named operations such as document publishing, inspection,
precise editing, and verification; it does not expose an arbitrary shell or raw
`lark-cli` command surface.

The bundled `.mcp.json` runs relative to the installed plugin root and sets
`UV_CACHE_DIR=/tmp/codex2lark-uv-cache`. That directory contains reusable Python
dependencies only. Feishu document content, edit plans, credentials, and
resource copies are not stored there.

### 5.2 Source development mode

Use this mode while running Codex2Lark directly from a cloned repository that
has not been installed as a plugin. Register the repository's launcher with
`codex mcp add`, as documented in [usage.md](usage.md#3-connect-codex2lark).

The registration is stored in the local Codex configuration; it is not a
repository-local or project-scoped configuration entry. Its launcher command
contains the repository's absolute path, so Codex still starts the correct
checkout regardless of the task's current working directory.

After either mode is configured, start a new Codex task or restart the Codex
host so the MCP tool inventory is rebuilt. Then inspect the configured server
with `codex mcp get codex2lark` or `codex mcp list`.

The complete registration command, verification procedure, example authoring
requests, tool inventory, shutdown procedure, and uninstall instructions are
maintained in [usage.md](usage.md). Keep this document focused on installation
and runtime operations.

## 6. First smoke test

Ask Codex to create a disposable Feishu document containing a heading, callout,
table, and Mermaid whiteboard. Confirm the response contains a live URL and a
passed read-back verification. Then ask it to make a surgical edit using that
URL and confirm unrelated text remains present.

Do not use a production document for the first test.

## 7. Managed folder and edit notifications

Codex2Lark creates Docs, Sheets, and Base resources inside a root Drive folder
named `Codex2Lark`. The folder is discovered from Feishu on demand and its token
is not written to local configuration. On the first creation request the server
creates the folder automatically. If more than one exact-name folder exists,
rename or remove the duplicate before retrying; the server deliberately refuses
to guess.

Managed-folder discovery requires Drive metadata read access, title-based
document discovery requires `search:docs:read`, and creating the managed folder
requires `space:folder:create`. Resource creation still requires the
corresponding Docs, Sheets, or Base scopes.

Verified document edits send a direct message as the application bot to the
current lark-cli user. Configure the application with
`im:message:send_as_bot`, ensure the bot is available to that user, and establish
a direct-message relationship with the bot before relying on notifications.
The message contains only the document identity, link, concise change summary,
and verification outcome.

Group-chat digest publishing additionally requires group/message read scopes
such as `im:chat:read`, `im:message:readonly`, and the user message-history
scope reported by lark-cli for the target chat. Selective image retrieval uses
the same readable message resource boundary. The workflow never downloads file
attachments.

When publishing with bot identity, also grant the bot
`im:chat.members:read` and `im:chat.members:write_only`. The bot must already be
in the group and must be allowed by the group's invitation policy to add the
current authenticated user. Groups restricted to owner/admin invitations may
require the bot to be an owner, administrator, or eligible creator bot. An
invitation awaiting approval is not treated as membership, so digest publishing
stops until approval is complete.

Notification delivery is a post-write side effect. If it fails, the MCP result
reports `notification.status = failed`; inspect the warning and fix bot access,
but do not rerun the document edit solely to obtain the message.

## 8. Troubleshooting

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
- `not_found_error` for a document title: check the exact title or provide its
  Feishu URL; title lookup searches managed content first and legacy Drive
  content second.
- `ambiguity_error` for a document title or folder: rename duplicates or provide
  an explicit document URL. The managed folder itself must have a unique exact
  name.
- notification failed after a successful edit: grant
  `im:message:send_as_bot`, make the bot available to the current user, establish
  a bot direct-message relationship, and test again with a new intentional
  edit. Do not replay the completed edit.
- group not found or ambiguous: provide the exact group name or its `chat_id`;
  the digest workflow never chooses a fuzzy group-name result.
- bot can see the group but cannot add the current user: grant the member read
  and write scopes, confirm the user is inside the application's availability
  range, and allow the bot to invite members (or have a group owner add the user
  manually). A pending invitation must be approved before retrying.
- incomplete group history: narrow the start/end range or intentionally raise
  the bounded page/message limits, then retry. No partial digest was created.
- startup timeout: run `uv sync` once in the installed plugin directory or
  increase the plugin-scoped MCP startup timeout.
- verification error: the write reached Feishu but the live resource did not
  satisfy the requested invariants; inspect it before retrying.
