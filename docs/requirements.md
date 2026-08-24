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
