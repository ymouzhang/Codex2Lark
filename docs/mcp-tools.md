# MCP tool contracts

MCP is the interactive semantic-tool surface of Codex2Lark. It is not the V2
Agent Harness control protocol and does not own production Feishu event
availability. Thread/run lifecycle, event streaming, steering, follow-up,
approval, cancellation, and recovery belong to the Harness Run API defined in
`agent-harness.md` and the standalone V3 Gateway in `architecture.md`.

## Server lifecycle

MCP owns only the interactive semantic-tool process. It does not start, stop,
or supervise Feishu event subscriptions. Real-time bot-added automation runs in
the independent `codex2lark gateway` process documented in `operations.md`.
The digest tool retains its live membership gate as an idempotent recovery
check, but MCP availability never defines event availability.

## 1. Design rules

- Tool names express Feishu business operations, not CLI commands.
- Inputs use strict JSON-compatible schemas and reject extra fields.
- Read and write tools are separate so side effects are clear.
- Tools return compact structured results; large document bodies are returned
  only by explicit inspection calls.
- A tool never accepts shell text, arbitrary argv, or a raw API path.
- Write tools include a verification policy and report its outcome.
- MCP annotations mark inspections/verifications read-only and all Feishu
  mutations as open-world writes. Precise edits and overwrites are additionally
  marked potentially destructive so the host can apply write approval policy.

## 2. Common types

### Identity

`user` is the default for Docs, Drive, Sheets, Base, and Whiteboard. `bot` is
allowed only when explicitly requested.

### Resource reference

```json
{
  "url": "https://example.feishu.cn/docx/token",
  "token": null
}
```

Exactly one of `url` or `token` is required.

### Tool result envelope

```json
{
  "ok": true,
  "resource": {},
  "verification": {
    "status": "passed",
    "checks": []
  },
  "warnings": []
}
```

Errors use a stable category and safe message. Raw upstream error data may be
included only after secret redaction.

## 3. First-release tools

### `feishu_docs_inspect`

Reads a live document as XML or Markdown.

Inputs:

- resource reference;
- `format`: `xml` or `markdown`;
- `detail`: `simple`, `with_ids`, or `full`;
- identity.

The default is XML with `full` detail for editing and Markdown with `simple`
detail for reading. The caller must choose explicitly in the MCP request.

### `feishu_docs_search`

Finds live `docx` resources by exact title. It searches the managed
`Codex2Lark` folder first and falls back to the whole visible Drive only when the
managed scope contains no exact match. The result reports the selected scope and
compact candidates containing title, token, URL, type, and update time.

The tool is read-only and never creates the managed folder. It checks at most
three pages using a title-only query. Callers must not choose among multiple
exact candidates without additional user direction.

### `feishu_docs_create`

Creates a document from Feishu XML or Markdown.

Inputs:

- title;
- format;
- content;
- identity;
- verification policy.

The destination is not caller-selectable: the server resolves or creates the
managed `Codex2Lark` Drive folder and passes its live token to lark-cli.

The implementation writes content through an ephemeral `@file` rather than a
shell argument.

### `feishu_docs_publish`

Compiles a typed `DocumentSpec` into Feishu XML, creates the document, and
performs the same live read-back verification as `feishu_docs_create`. This is
the preferred creation tool for AI-authored rich documents; raw XML remains an
advanced escape hatch.

`DocumentSpec` contains a title and a bounded ordered list of discriminated
blocks. The first release accepts headings, paragraphs, lists, checkboxes,
quotes, callouts, code, tables, dividers, images, bookmarks, whiteboards, and
Sheet resource blocks. Rich text is represented as typed spans rather than
embedded markup. URLs, colors, alignment, table spans, and nesting are
validated before XML generation.

The compiler owns escaping and the Feishu-mandated inline-style nesting order.
The generated XML is never returned or stored after the request completes.

### `feishu_docs_edit`

Applies a bounded list of precise operations to a live document.

The target is exactly one of a resource reference or `document_title`. A title
is resolved with the same managed-folder-first policy as
`feishu_docs_search`; zero exact matches return `not_found_error` and multiple
exact matches return `ambiguity_error` with compact candidates.

Allowed first-release operations:

- `append`;
- `overwrite`;
- `str_replace` with an exact old value;
- `block_insert_after`;
- `block_replace`;
- `block_delete`.

Block operations require a block ID obtained from a live inspection in the same
agent workstream. `overwrite` is high impact and must be explicitly selected.

Before every operation, the service checks a fresh enough live snapshot. Exact
string replacement without a block selector must match once: zero matches is a
conflict and multiple matches is an `ambiguity_error`. A supplied block ID must
exist in the current XML snapshot. After replacement or deletion, the next
operation refetches live XML before validating any reused block ID.

The request may include `expected_revision`. When the selected lark-cli command
cannot enforce it atomically, the server performs a just-in-time revision check
and returns a warning that the guarantee is best-effort.

The request also requires `change_summary`, a concise user-facing description
of what the authorized edit changes. After read-back verification, the service
sends one idempotent bot direct message to the current authenticated user with
the document title/link, this summary, and verification status. The result
contains `notification.status` (`sent` or `failed`). A failed notification does
not turn the already verified edit into an error and must not trigger an edit
retry.

### `feishu_docs_verify`

Reads a document back and checks requested invariants:

- title;
- required text fragments;
- forbidden text fragments;
- minimum block counts by supported type;
- current revision when returned by Feishu.

### `feishu_chat_digest_publish`

Creates a verified chronological document from one explicit group-chat time
range.

Inputs:

- exactly one of `chat_id` or exact `chat_name`;
- required `start` and `end` values accepted by lark-cli (ISO 8601 or date);
- bounded `page_limit` and `max_messages` safeguards;
- IANA timezone used for rendered timestamps, default `Asia/Shanghai`;
- chat identity used for group discovery, message reading, and image retrieval,
  default `user`; document authoring always uses the current user identity;
- verification policy.

The tool resolves the live group name and uses it as the exact document title.
Group-name lookup continues only for one normalized exact match. Messages and
expanded thread replies are de-duplicated and sorted by creation time. If
auto-pagination reports incomplete traversal or the normalized message count
exceeds `max_messages`, the tool fails before creating a folder or document.

With `identity = bot`, the exact group must contain both the configured bot and
the current lark-cli user. The tool resolves the authenticated user's `open_id`
from live auth status, checks the complete user-member list as the bot, invites
only that user when confirmed absent, and verifies access as the user before
continuing. This membership write requires `im:chat.members:read` and
`im:chat.members:write_only`, may be rejected by group invitation policy, and
is part of the declared external-write behavior of this tool. Pending approval,
incomplete membership inspection, and failed verification are hard failures;
no message history or document is produced.

Bot chat access never changes document ownership. Managed-folder search/create,
digest create/update, and document verification run as `user`, and the result
reports both `chat.identity` and `author_identity` explicitly.

The managed folder holds one canonical digest for the exact group name. The tool
creates it when absent. It refreshes an existing match only after live
inspection confirms the `群聊记录` digest marker; duplicate matches or an
unmarked same-title document are conflicts, never overwrite candidates. A
refresh rechecks the marker and revision immediately before overwrite, and a
verified refresh sends the standard bot edit notification.

Image resources are downloaded one at a time with resource type `image` into
the request workspace and embedded in the document. File messages render the
metadata filename plus `not downloaded`; their `file_key` is never sent to a
download operation. Image failures become explicit transcript placeholders and
warnings rather than causing file downloads or silent omission.

The result reports `action` (`created` or `updated`), the live document, managed
folder, chat identity, user-membership outcome, requested range, normalized
message/image/file counts, verification and notification results, and warnings.

### `feishu_whiteboard_render`

Creates or updates a document whiteboard from Mermaid, PlantUML, or SVG. The
first release supports source-based render/update; arbitrary raw node mutation
is deferred.

### `feishu_sheets_create`

Creates a workbook with one or more sheets and optionally typed cell data and
styles. The exact lark-cli payload is server-generated from the strict request
schema. The workbook is created in the managed `Codex2Lark` Drive folder.

### `feishu_sheets_write`

Writes a bounded rectangular range using typed cells. Formula writes are read
back for verification.

### `feishu_base_create`

Creates a Base application and optional tables. It does not expose arbitrary
Base automation or permission APIs in the first release. The Base application
is created in the managed `Codex2Lark` Drive folder.

### `feishu_base_upsert_records`

Upserts bounded record batches into a specified table using caller-supplied
record IDs or a declared unique field strategy supported by the adapter.

## 4. Tool composition

Creating a rich document is an agent-orchestrated workflow, not one oversized
MCP call:

1. create supporting whiteboard/Sheet/Base resources as needed;
2. use returned tokens in the document XML;
3. create or edit the document;
4. inspect and verify the final live document.

Independent read-only inspections may run concurrently. Writes to the same
document must be serialized because each can invalidate revision and block IDs.

## 5. Deferred tools

- Drive permission mutation;
- Wiki node creation and movement;
- chart-specific Sheet tools;
- arbitrary whiteboard node editing;
- document history rollback;
- remote asynchronous job tools.

These require a documentation update before implementation.
