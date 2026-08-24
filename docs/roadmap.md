# Delivery roadmap

## Phase 0: capability foundation

- Initialize the repository as a Codex plugin and `uv` Python project.
- Add the Feishu authoring Skill and progressive references.
- Add a local stdio MCP server.
- Implement safe `lark-cli` execution, doctor checks, and normalized errors.

Exit: Codex can discover the plugin and list its tools without Feishu writes.

## Phase 1: document create and inspect

- Implement strict models for resource references and identities.
- Implement document inspect and create.
- Add ephemeral `@file` rendering.
- Add live read-back verification.

Exit: an authorized user can create a structured document and receive a verified
URL.

## Phase 2: precise document editing

- Implement the bounded edit operation set.
- Add live block/revision inspection and target resolution.
- Add conflict and ambiguity results.
- Add protected-text verification.

Exit: an agent can modify a requested scope without whole-document replacement.

## Phase 3: rich artifacts

- Implement whiteboard render/update.
- Implement Sheet create/write and formula verification.
- Implement Base create and bounded record upsert.
- Compose artifacts into a final document.

Exit: an agent can create a document containing live Feishu-native artifacts.

## Phase 4: remote plugin transport

- Add Streamable HTTP transport.
- Add production OAuth/secret-provider integration.
- Add optional operation preview/confirmation UI.
- Add deployment and threat-model documentation before code.

Exit: ChatGPT and remote Codex environments can use the same semantic tools.

## Current implementation boundary

The initial implementation in this repository delivers the local stdio scope of
Phases 0–3 with testable adapters. It includes typed document compilation,
bounded exact/block editing, whiteboard rendering, Sheet operations, and Base
operations. Live Feishu execution requires the user to install and authenticate
`lark-cli`; default tests use deterministic subprocess and service doubles.

Semantic heading-path selectors and cross-session three-way merge remain
deferred; exact text and live block IDs are the supported precise selectors.
