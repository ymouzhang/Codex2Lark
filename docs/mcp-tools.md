# MCP tool contracts

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

### `feishu_docs_create`

Creates a document from Feishu XML or Markdown.

Inputs:

- title;
- format;
- content;
- optional folder token;
- identity;
- verification policy.

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

### `feishu_docs_verify`

Reads a document back and checks requested invariants:

- title;
- required text fragments;
- forbidden text fragments;
- minimum block counts by supported type;
- current revision when returned by Feishu.

### `feishu_whiteboard_render`

Creates or updates a document whiteboard from Mermaid, PlantUML, or SVG. The
first release supports source-based render/update; arbitrary raw node mutation
is deferred.

### `feishu_sheets_create`

Creates a workbook with one or more sheets and optionally typed cell data and
styles. The exact lark-cli payload is server-generated from the strict request
schema.

### `feishu_sheets_write`

Writes a bounded rectangular range using typed cells. Formula writes are read
back for verification.

### `feishu_base_create`

Creates a Base application and optional tables. It does not expose arbitrary
Base automation or permission APIs in the first release.

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
