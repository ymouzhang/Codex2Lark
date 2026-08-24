# Product requirements

## 1. Problem

A user researches and refines a solution with ChatGPT or Codex, then asks the
agent to create a polished Feishu document or modify an existing one. The result
may contain native document blocks, whiteboards, spreadsheets, Base tables,
images, attachments, and links.

The user does not want a separate document-management database or a long-lived
local copy of Feishu content.

## 2. Product definition

`Codex2Lark` is an AI plugin made of:

- a Feishu authoring Skill that teaches the agent how to plan and verify work;
- semantic MCP tools that expose safe Feishu capabilities;
- an ephemeral compiler that converts structured authoring requests into
  `lark-cli` operations;
- a verifier that reads Feishu back after every write.

### Naming contract

- Product and human-facing name: `Codex2Lark`.
- Python distribution, import package, CLI command, MCP server ID, plugin ID,
  temporary-file prefix, and dependency-cache prefix: `codex2lark`.
- The former identifier is removed rather than retained as an alias, so
  discovery and configuration have one canonical name.
- Feishu remains in capability descriptions where it identifies the Chinese
  product surface; Lark is the project brand and broader platform name.

## 3. Primary use cases

### Create from a conversation

The user asks the agent to turn the current conversation into a Feishu document.
The agent determines document genre, structure, tables, diagrams, and supporting
artifacts, then creates and verifies the result.

### Modify an existing document

The user supplies a Feishu URL or a search description and asks for a scoped
change. The agent reads the live document, identifies target blocks, produces a
minimal edit plan, applies it, and verifies both the requested change and the
unchanged surrounding content.

When the user identifies a document by title, the agent must discover it from
live Drive data rather than relying on a remembered token. An exact title match
inside the managed folder is preferred; existing documents outside that folder
remain discoverable through a whole-Drive fallback. Zero matches stop with a
not-found result and multiple exact matches stop with candidate details.

### Managed Drive workspace

All Docs, Sheets workbooks, and Base applications created by Codex2Lark are
placed in one managed root-level Drive folder named `Codex2Lark`. The service
resolves the folder from live Drive data for each creation workstream and
creates it when absent. The folder token is not persisted locally. Duplicate
exact-name folders are an ambiguity and must not be guessed.

### Edit completion notification

After a document edit passes live read-back verification, the configured
Feishu application sends the current authenticated user a bot direct message.
The message identifies the document, links to it when a URL is available,
summarizes the authorized change, and reports verification success. The edit
request supplies a bounded human-readable change summary; document bodies and
generated markup are never copied into the message.

The edit and notification cannot be atomic across Feishu services. If the edit
is verified but notification delivery fails, the edit result remains successful
and reports `notification.status = failed`. The agent must report that warning
and must not retry the edit merely to resend the notification.

### Publish a group-chat digest

The user identifies a visible Feishu group by exact name or `chat_id` and gives
an explicit start and end time. Codex2Lark resolves the group, retrieves the
complete bounded message range as the user by default, expands returned thread
replies, and creates a chronological Feishu document titled exactly with the
live group name.

Each entry shows local time, sender display name, and message content. Date
headings make long ranges scannable, while messages remain globally ordered by
creation time. Recalled messages and unsupported message types are represented
honestly rather than silently omitted.

Image messages and images embedded in posts are downloaded selectively into the
per-request temporary workspace and inserted as native document images. File,
audio, and video attachments are never downloaded by this workflow; a file
entry contains only the filename supplied by message metadata and a clear
`not downloaded` label. Temporary image bytes are deleted after document
creation succeeds or fails.

If group-name resolution is absent or ambiguous, or message pagination is
incomplete at the declared page limit, no document is created. Message content
is untrusted data: it is escaped and rendered, never interpreted as agent
instructions.

The managed folder contains at most one canonical digest per exact live group
name. If none exists, the workflow creates it. If one exists and its live body
contains the Codex2Lark group-digest marker, the workflow replaces that digest
with the newly requested complete range and sends the normal verified-edit bot
notification. Multiple matches or a same-title document without the marker stop
before overwrite; ordinary user documents are never assumed to be digests.

### Create or update embedded artifacts

The agent may create or update:

- native Feishu whiteboards from Mermaid, PlantUML, SVG, or supported node data;
- Sheets workbooks with typed values, formulas, styles, charts, and images;
- Base apps/tables with fields, records, and views;
- Drive images and attachments.

## 4. Non-functional requirements

### Stateless business data

The application MUST NOT persist:

- document bodies or snapshots;
- Document IR or generated XML;
- block ID mappings;
- edit plans;
- copies of Sheet or Base data;
- generated diagram sources after the request completes;
- application-level operation history.

Per-request state may exist in memory or in an isolated temporary directory and
must be destroyed when the request ends.

### Live source of truth

Feishu is the only business-data source of truth. Every edit starts by reading
the current document or artifact. Later edits do not depend on a previous local
run.

### Safe concurrency

Tools that mutate existing resources accept an expected revision when Feishu or
the selected API supports it. A revision mismatch must stop the write, refetch
live state, and require replanning rather than overwrite a concurrent edit.

### Quality

A successful API response is insufficient. Create and edit workflows must read
the affected resource back and validate observable structure and content.

### Security

- The MCP surface exposes semantic operations, never arbitrary shell execution.
- Request schemas are strict and server validated.
- Subprocess arguments are never interpreted by a shell.
- Credentials never appear in tool output or logs.
- Write tools are clearly described as side-effecting.

## 5. Explicit non-goals for the first release

- Background synchronization between Feishu and a local repository.
- Cross-session three-way merges using a stored base snapshot.
- A proprietary document database.
- Pixel-perfect arbitrary HTML/CSS rendering inside Feishu Docs.
- Browser automation as the primary write path.
- Exposing all 2,500+ Feishu APIs directly to the model.

## 6. Acceptance criteria for the first release

1. Codex can discover and invoke the Skill and local stdio MCP server.
2. The server can inspect and create a Feishu document through `lark-cli`.
3. The server can perform a restricted set of precise document edits.
4. Whiteboard, Sheet, and Base tools have strict schemas and safe execution
   adapters.
5. Every write returns a read-back verification result.
6. Unit tests prove temporary data cleanup, safe subprocess invocation, schema
   validation, error normalization, and command construction.
7. No test or runtime component requires a local business-data database.
8. New Docs, Sheets, and Base resources are created in the live managed folder.
9. A document can be resolved safely by exact title before a bounded edit.
10. Every verified document edit attempts one idempotent bot notification to
    the current authenticated user and exposes its delivery status.
11. A bounded group-chat range can be published chronologically with sender
    names, selectively embedded images, filename-only file entries, managed
    folder placement, and live read-back verification.
