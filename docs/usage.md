# Using Codex2Lark from Codex

This guide explains how to connect the local Codex2Lark stdio MCP server to
Codex and use natural-language requests to create and precisely edit Feishu
documents, whiteboards, Sheets, and Base applications.

## 1. Understand the stdio process

Running the following command directly starts an MCP protocol process:

```bash
uv run codex2lark mcp
```

It does not start an interactive shell or chat interface. A terminal that stays
open without printing a prompt is normally waiting for an MCP client on stdin.
Press `Ctrl+C` to stop a manually started process. In normal use, Codex launches
and manages this child process; users do not keep it running themselves.

While this process is running, it also listens for the Codex2Lark bot being
added to a group and immediately ensures that the current authenticated user is
in that group. Enable `im.chat.member.bot.added_v1` in the Feishu developer
console and grant `im:chat.members:bot_access`, `im:chat.members:read`, and
`im:chat.members:write_only`. Stopping MCP stops this real-time automation; no
event content or replay checkpoint is stored locally.

## 2. Verify the runtime

From the repository root, prepare dependencies and verify the pinned lark-cli
runtime and Feishu authentication:

```bash
uv sync --all-groups
uv run codex2lark doctor
```

A healthy result reports:

- `ok: true`;
- `lark_cli: available`;
- `lark_cli_version: 1.0.89`;
- an available authenticated identity;
- `business_data_persistence: disabled`.

See [operations.md](operations.md) if installation, authentication, or version
verification fails.

## 3. Connect Codex2Lark

Choose exactly one connection mode:

- **Installed plugin mode:** Codex reads the plugin manifest, packaged Skill,
  and bundled `.mcp.json`, then manages the MCP child process automatically. Do
  not also add a duplicate manual server entry.
- **Source development mode:** use `codex mcp add` to point the local Codex
  configuration at the cloned repository. This is the appropriate mode while
  developing or testing this checkout.

The following command is only for source development mode.

### Register a source checkout

From the cloned repository root, capture its absolute path and register the
stdio launcher:

```bash
CODEX2LARK_PROJECT="$(pwd)"

codex mcp add codex2lark \
  --env UV_CACHE_DIR=/tmp/codex2lark-uv-cache \
  -- uv run \
  --project "$CODEX2LARK_PROJECT" \
  codex2lark mcp
```

The bundled plugin configuration in [`.mcp.json`](../.mcp.json) describes the
same server for installed plugin mode. Direct `codex mcp add` registration is
the simplest source development setup.

Codex stores this manually registered MCP server in its local configuration,
not in the repository. The launcher includes the repository's absolute path, so
the server remains tied to this checkout. The command shape and `--env` behavior
follow the
[official Codex developer commands documentation](https://learn.chatgpt.com/docs/developer-commands#codex-mcp).

## 4. Confirm registration

Inspect the registered server:

```bash
codex mcp get codex2lark
codex mcp list
```

After registration, start a new Codex task or restart the Codex host so its MCP
tool inventory is rebuilt. A task that was already running before registration
does not necessarily gain the new tools dynamically.

The server should expose these semantic operations:

| Tool | Purpose | Side effect |
|---|---|---|
| `feishu_docs_search` | Find exact document titles, managed folder first | Read-only |
| `feishu_docs_inspect` | Read a live document before editing | Read-only |
| `feishu_docs_create` | Create a document from advanced XML or Markdown | Creates a document |
| `feishu_docs_publish` | Compile and publish a typed rich document | Creates a document |
| `feishu_docs_edit` | Apply bounded, precise edits | Mutates a document |
| `feishu_docs_verify` | Check live semantic and structural invariants | Read-only |
| `feishu_chat_digest_publish` | Publish a chronological group-chat range | Reads chat and creates a document |
| `feishu_whiteboard_render` | Create or update a source-based whiteboard | Mutates a whiteboard |
| `feishu_sheets_create` | Create a typed workbook | Creates a workbook |
| `feishu_sheets_write` | Write a bounded range and verify formulas | Mutates a workbook |
| `feishu_base_create` | Create a Base application and tables | Creates a Base |
| `feishu_base_upsert_records` | Create or update a bounded record batch | Mutates a Base |

## 5. Use natural-language requests

Users should describe the desired business result, document audience, required
structure, supporting artifacts, and verification expectations. Codex selects
the semantic tools and handles Feishu-specific implementation details.

### Publish a technical proposal

```text
把我们刚才讨论的 Codex2Lark 架构方案整理成一篇专业的飞书文档。
要求：
1. 包含背景、目标、架构、组件职责、数据流和实施计划
2. 使用表格整理组件职责
3. 使用 Mermaid 架构图
4. 排版适合技术方案评审
5. 创建后读取飞书文档进行验证
```

Expected workflow:

1. organize the conversation into a typed document specification;
2. create supporting resources such as the Mermaid whiteboard;
3. resolve or create the managed `Codex2Lark` Drive folder;
4. publish the document in that folder;
5. read the live document back;
6. verify required sections, tables, resource references, and title;
7. return the live Feishu URL and verification status.

### Precisely edit an existing document

```text
读取这个飞书文档：
https://example.feishu.cn/docx/example-token

在“部署方案”章节后增加“故障恢复”章节，不要改动其他内容。
修改完成后重新读取并验证。
```

Codex should inspect the live document first, use a bounded edit operation,
serialize writes to the document, and verify that the requested change exists
while unrelated content remains intact. After verification, the Feishu bot
sends the current authenticated user a direct message containing the document,
change summary, and verification outcome.

The document may also be identified by exact title:

```text
帮我修改“Codex2Lark 架构方案”文档：
在“部署方案”章节后增加“故障恢复”章节，不要改动其他内容。
修改完成后重新读取验证，并由飞书机器人告诉我修改了什么。
```

Codex searches the managed folder first and then the visible Drive for legacy
documents. It proceeds only for one exact title match; otherwise it reports no
match or asks the user to choose among duplicate candidates.

### Create a Sheet

```text
创建一个飞书电子表格，用于跟踪 Codex2Lark 开发计划。
包含任务、负责人、状态、优先级、开始时间和截止时间，
并填入当前讨论中已经确定的任务。创建后读取公式和关键单元格进行验证。
```

### Create a Base application

```text
创建一个飞书多维表格管理产品需求。
包含需求名称、模块、优先级、状态、负责人和验收标准，
并将当前讨论中的需求写入记录。
```

### Create a document with an architecture whiteboard

```text
把当前方案发布成飞书技术评审文档，同时创建一张 Mermaid 架构图画板并嵌入文档。
完成后读取文档，确认画板不是空白占位符，并返回文档 URL。
```

### Publish a group-chat digest

```text
把“Codex2Lark 项目群”从 2026-08-20 到 2026-08-24 的群聊消息整理成飞书文档。
标题使用群名称，按时间顺序列出说话人和内容；图片插入原位置；
文件只显示文件名，不要下载。创建后读取文档验证。
```

Codex resolves one exact group, confirms the bounded range is completely
retrieved, and publishes the digest in the managed folder. A missing or
duplicate group, incomplete pagination, or excessive message count stops before
document creation.

## 6. Safety and data behavior

- A write requires clear user intent. Codex may request approval before an
  external mutation.
- User identity is the default for personal Feishu resources unless bot
  ownership is explicitly requested.
- Feishu is the only business-data source of truth. Document content, block
  mappings, and edit plans are not persisted locally.
- Request-local temporary files are deleted after success, failure,
  cancellation, or timeout.
- Edits inspect live content before writing. Writes are followed by live
  read-back verification.
- New Docs, Sheets, and Base resources are placed in the managed
  `Codex2Lark` Drive folder without persisting its token locally.
- Verified document edits attempt one idempotent bot direct message. A message
  delivery failure is reported but never causes the completed edit to be
  replayed.
- Use a disposable document for the first end-to-end test instead of a
  production document.

## 7. Stop, disconnect, or uninstall

Stopping a running process, removing a source-development registration, and
uninstalling a plugin are different operations. Use the action that matches the
connection mode selected in section 3.

### Stop a manually started MCP process

If a terminal is currently waiting after this command:

```bash
uv run codex2lark mcp
```

press `Ctrl+C` in that terminal. This only stops that process. It does not
remove a Codex MCP registration, uninstall a plugin, delete the repository, or
log out of Feishu.

### Stop a Codex-managed MCP process

Codex starts the stdio child process when it loads the configured MCP server and
manages that process for the task or host session. Normally, finish the task or
close/restart the Codex host instead of killing the child process manually.

To prevent Codex from starting Codex2Lark again, remove the configuration that
owns it:

- source development mode: remove the manual MCP registration;
- installed plugin mode: uninstall the plugin;
- if both were configured accidentally: remove both to avoid duplicate tools.

### Remove a source-development registration

Remove the server entry created by `codex mcp add`:

```bash
codex mcp remove codex2lark
```

Verify that it is gone:

```bash
codex mcp list
codex mcp get codex2lark
```

The second command should report that no server named `codex2lark` exists.
Start a new task or restart the Codex host so an already loaded tool inventory
is discarded.

This operation only removes the entry from the local Codex configuration. It
does not delete the cloned repository, the uv environment, lark-cli, Feishu
credentials, or any Feishu resources previously created by the user.

### Uninstall an installed plugin

First list installed plugins and identify the exact Codex2Lark marketplace
name:

```bash
codex plugin list --json
```

Then remove the plugin using either supported selector form:

```bash
codex plugin remove codex2lark@MARKETPLACE
```

or:

```bash
codex plugin remove codex2lark --marketplace MARKETPLACE
```

Replace `MARKETPLACE` with the marketplace name returned by
`codex plugin list --json`. Restart the Codex host or start a new task after
removal. If the source-development MCP entry was also added manually, run
`codex mcp remove codex2lark` as a separate cleanup step.

Plugin removal deletes the installed plugin entry and its Codex-managed plugin
cache. It does not delete the original Git checkout, uninstall the shared
`@larksuite/cli` runtime, revoke Feishu authorization, or delete existing
Feishu documents.

### Optional dependency-cache cleanup

The directory `/tmp/codex2lark-uv-cache` contains downloaded Python dependency
artifacts only. It is safe to leave in place and may speed up a later reinstall.
Delete it only when a full local dependency-cache cleanup is desired:

```bash
rm -rf /tmp/codex2lark-uv-cache
```

Do not delete the source repository until any uncommitted work has been reviewed
and preserved. Removing Codex2Lark never requires deleting user-created Feishu
resources.

The `mcp remove` and `plugin remove` command families are documented in the
[official Codex developer commands reference](https://learn.chatgpt.com/docs/developer-commands#command-overview).

## 8. Troubleshooting

### The terminal appears stuck after `codex2lark mcp`

This is expected for a manually started stdio server. Stop it with `Ctrl+C`,
register it with `codex mcp add`, and let Codex manage the process.

### `codex mcp get codex2lark` says the server does not exist

Run the registration command in section 3. Registration is per local Codex
configuration and is separate from merely running the MCP process once.

### The server is registered but tools are absent

Start a new Codex task or restart the Codex host, then run `codex mcp list` and
confirm that `codex2lark` is enabled. Also run `uv run codex2lark doctor` from
the repository root.

### The server fails to start

Use an absolute path after `uv run --project`, confirm `uv` is on the Codex
host's `PATH`, and run `uv sync --all-groups` once. The bundled cache location
contains dependencies only and may safely remain at
`/tmp/codex2lark-uv-cache`.

### Authentication, permission, or version checks fail

Follow [operations.md](operations.md#7-troubleshooting). Codex2Lark requires
`@larksuite/cli@1.0.89`; a different installed version intentionally fails the
doctor check.
