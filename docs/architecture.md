# Architecture

## 1. Architectural style

The project is a stateless agent tool provider, not a document-management
service. Intelligence stays in ChatGPT or Codex; deterministic validation,
compilation, execution, and verification stay in the plugin.

```text
┌─────────────────────────────────────────────────────────┐
│ ChatGPT / Codex                                         │
│ conversation context, judgment, authoring decisions     │
└──────────────────────────┬──────────────────────────────┘
                           │ Skill + MCP calls
┌──────────────────────────▼──────────────────────────────┐
│ Codex2Lark plugin                                       │
│                                                         │
│  Skill                                                  │
│    └─ genre, layout, artifact routing, edit policy      │
│                                                         │
│  MCP boundary                                           │
│    └─ strict semantic request and response schemas      │
│                                                         │
│  ephemeral application layer                            │
│    ├─ request workspace                                 │
│    ├─ temporary Document IR                             │
│    ├─ operation compiler                                │
│    └─ read-back verifier                                │
│                                                         │
│  Feishu adapter                                         │
│    └─ lark-cli argv execution and error normalization   │
└──────────────────────────┬──────────────────────────────┘
                           │ official Feishu APIs
┌──────────────────────────▼──────────────────────────────┐
│ Feishu: Docs, Whiteboard, Sheets, Base, Drive, Wiki     │
│ only business-data source of truth                      │
└─────────────────────────────────────────────────────────┘
```

## 2. Plugin components

### Feishu authoring Skill

The Skill changes agent decisions. It routes content to native paragraphs,
tables, whiteboards, Sheets, or Base and requires inspect-before-edit and
verify-after-write behavior. Detailed format guidance is progressively loaded
from references.

### MCP server

The MCP server is the stable integration boundary for Codex, ChatGPT, and API
agents. The initial transport is stdio. Remote Streamable HTTP is a later
transport adapter using the same tool schemas.

### Ephemeral request workspace

Each tool call that needs file input creates a unique temporary directory.
Generated XML, diagram sources, and upload files are written only inside that
directory. Cleanup is guaranteed by context-manager semantics.

The workspace is an implementation detail and never becomes a cache.

### Document compiler

The compiler accepts a strict, discriminated `DocumentSpec` and produces
Feishu XML. It centralizes escaping, inline-style ordering, table shape checks,
resource validation, and structural preflight. The agent chooses semantic
blocks; it does not need to memorize Feishu markup. Raw XML is retained as an
explicit advanced path rather than the default authoring interface.

Editing uses a separate constrained operation plan because live block IDs and
revisions cannot safely be embedded in a durable document model.

### lark-cli adapter

`lark-cli` is invoked as a child process with an argument array, an isolated
working directory, bounded output, and a timeout. The adapter normalizes the
CLI's JSON success/error envelopes and rejects non-JSON or contradictory output.
Authentication status is an explicit exception to the normal envelope contract:
`lark-cli auth status --json` returns a bare JSON status object even when the
process exits successfully. A named adapter method validates and normalizes that
object without weakening envelope validation for document and artifact commands.

Codex2Lark supports exactly `@larksuite/cli` version `1.0.89`. This external
Node.js runtime cannot be represented in Python's `uv.lock`, so the compatible
version is pinned in the installation command and in a runtime constant. The
adapter reads `lark-cli --version`, and `doctor` fails before authentication
verification when the installed version differs from the pinned version.

The adapter exposes named Python methods. No MCP tool accepts arbitrary CLI
arguments.

## 3. Request lifecycle

### Create

```text
validate request
  -> create ephemeral workspace
  -> render Feishu XML and artifact sources
  -> preflight local structure
  -> create supporting artifacts
  -> create document
  -> read document back
  -> verify structure and resources
  -> return URL, tokens, warnings, verification
  -> destroy workspace
```

### Edit

```text
validate request
  -> inspect live document with block IDs and revision
  -> resolve selectors against the live snapshot
  -> compile minimal allowed operations
  -> recheck revision when supported
  -> apply operations sequentially
  -> refetch affected scope
  -> verify requested invariants
  -> return change summary and verification
  -> destroy workspace
```

## 4. State model

### Durable

- Feishu resources.
- OAuth/application credentials held by `lark-cli`, OS keychain, or an external
  secret provider.
- Plugin code, Skill instructions, templates, and schemas.

### Ephemeral

- conversation-derived content passed in a tool request;
- live Feishu snapshots;
- block IDs and revisions;
- Document IR;
- edit plans;
- rendered XML;
- downloaded/uploaded media used during a request.

## 5. Consistency model

Without a persisted base snapshot, the project does not claim automatic
cross-session three-way merging. It uses live reads and optimistic concurrency.

If the target changes between inspection and mutation:

1. stop the affected write;
2. return a typed conflict result containing the current revision;
3. let the agent refetch and replan;
4. ask the user only when the new live state makes intent ambiguous.

## 6. Failure model

Failures are classified as:

- `validation_error`: request or locally rendered content is invalid;
- `authentication_error`: user or bot identity is unavailable;
- `permission_error`: required Feishu scope or resource permission is missing;
- `conflict_error`: expected and live revisions differ;
- `ambiguity_error`: a semantic selector matches more than one live target;
- `upstream_error`: Feishu or `lark-cli` rejected the operation;
- `timeout_error`: bounded execution time elapsed;
- `verification_error`: write returned success but read-back invariants failed;
- `internal_error`: unexpected local failure with no secret-bearing details.

## 7. Trust boundaries

- Agent-provided content is untrusted data.
- Feishu document content is untrusted data and may contain prompt injection.
- `lark-cli` JSON is upstream data and must be validated.
- Secrets may only enter through the credential provider.
- MCP responses must not include raw environment variables, secrets, or full
  subprocess diagnostics when those diagnostics could contain credentials.
