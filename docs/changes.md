# Documentation-driven change log

This file records behavioral implementation work and the document that authorized
it. It is not a release changelog.

- Defined durable active-run controls before implementation: deterministic,
  same-requester IM classification; transactional pre-ACK event/control/outbox
  admission; safe-boundary Harness consumption; checkpointed applied IDs;
  restart-safe steering/follow-up; and root cancellation/interruption.
  Implementation area: IM admission, runtime control inbox, Harness checkpoints,
  graph lifecycle, atomic control-aware terminal closure, SQLite migration, and
  end-to-end recovery tests.

- Required durable workers to timestamp retry/terminal transitions with a
  fresh post-execution clock rather than the earlier lease-acquisition clock.
  Implementation area: task worker timing and acknowledgement-before-terminal
  outbox acceptance.

- Bounded the interactive `doctor` version/authentication probe by one
  20-second deadline and defined a content-safe timeout result with an operator
  next action. Implementation area: CLI diagnostics only; authoring-operation
  timeouts are unchanged.

- Separated the normative V3 target from the shipped production baseline and
  recorded an explicit delivered/partial/open matrix. README and architecture
  claims now point to that matrix instead of implying that isolated primitives
  satisfy end-to-end acceptance. Implementation area: documentation and package
  identity only; no runtime behavior changed.

- Defined managed-folder document-title uniqueness at create time: an exact
  existing managed match is a pre-write conflict, while same titles elsewhere
  in Drive do not block creation. Implementation area: DocsService create
  preflight and contract tests.

- Narrowed parallel delegation to children whose selected capabilities are all
  read-only. Writer children now execute serially until delegation includes
  trusted explicit targets and obtains durable resource locks; free-form task
  prose is never treated as proof of disjoint writes. Implementation area:
  argument-aware parallel-safety guard in ToolRegistry and DelegateAgentTool.

- Removed the volatile queue between official Feishu long-connection callbacks
  and SQLite admission. Message and bot-added callbacks now return only after
  their idempotent event/task/acknowledgement admission transaction commits;
  slow Agent work remains asynchronous. Implementation area: official Channel
  event source lifecycle and admission tests.

- Defined explicit `parallel_safe` tool metadata and deterministic concurrent
  execution for all-read, all-safe call batches; enabled it for independent
  `agent.delegate` calls while keeping Feishu mutation tools serial and
  idempotent. Implementation area: ToolDefinition, Harness scheduling, and
  delegation metadata.

- Split diagnostics into an accurate interactive lark-cli/auth check and a
  secret-safe `doctor --gateway` local preflight for V3 configuration, bundled
  resources, key shape, and existing storage integrity; removed the obsolete
  claim that all business-data persistence is disabled. Implementation area:
  CLI diagnostics and operator documentation.

- Defined importable, strictly validated, versioned production resource and IM
  response-template packages, selected explicitly by root/worker
  AgentDefinitions and checkpointed by version rather than hard-coded in the
  Gateway composition root. Implementation area: ResourceLoader, bundled
  package data, and V3 composition.

- Declared the durable V3 Gateway as the only executable group-runtime
  semantics and removed the superseded V2 `realtime` package, volatile broker,
  composition root, and their implementation-specific tests. Historical
  architecture research remains clearly non-normative.

- Restricted durable child artifacts with any Feishu business-data capability
  to content-free completion/verification/resource references; document,
  Sheet, Base, Whiteboard, and Drive-derived summaries and claims must be
  refetched live instead of being persisted locally. Implementation area:
  delegated Harness artifact projection.

- Defined production semantic-write idempotency using deterministic operation
  keys, durable pre-write claims, completed-reference replay, and mandatory
  live reconciliation after ambiguous interruption; inconclusive effects block
  instead of being blindly repeated. Implementation area: ToolExecutor,
  RuntimeStore, and write-tool reconciliation hooks.

- Defined an explicitly confirmed, stopped-Gateway, bounded retention pass for
  due event payloads, IM mirrors and parser output, artifacts, idempotency rows,
  and reference-safe encrypted blob reclamation. Implementation area: storage
  maintenance and CLI administration.

- Defined content-safe storage status and a stopped-Gateway backup,
  verification, and restore workflow with a hashed archive manifest, referenced
  encrypted blobs, no embedded key, and safe empty-directory restore.
  Implementation area: storage maintenance and CLI administration.

- Defined crash-safe retry exhaustion for durable tasks: an expired final lease
  now enters a repeatable terminal-finalization step instead of becoming a
  silent failed row. Implementation area: task leasing and durable worker
  failure rendering.

## 2026-08-24

- Defined production attachment-context composition before implementation:
  trigger and selected-message references are downloaded with their trusted
  message binding, encrypted in the managed blob store, safely parsed, and
  emitted as attributed evidence with explicit warnings.

- Defined production multi-Agent composition before implementation: every IM
  task prepares a durable root graph, `agent.delegate` creates or reuses a
  bounded child with subset authority, a separate child Harness commits an
  encrypted typed artifact, and only the root closes the graph and replies.

- Defined the V3 bot-added membership path before implementation: the official
  Channel callback enters a bounded queue, durable admission creates one
  replay-safe membership task, and the configured current user is added and
  live-access verified immediately without Codex/MCP.

- Defined the V3 authoring-artifact plugin before implementation: bounded
  Whiteboard, Sheets, and Base semantic tools share operator-bound identity,
  strict validation, write verification, and non-persistable observations.

- Defined the first V3 authoring-plugin tool profile before implementation:
  bounded search/inspect/create/edit operations, operator-bound delegated
  identity, live read-back verification, and checkpoint redaction of document
  bodies with mandatory refetch after recovery. Clarified causal result
  delivery: acknowledgement precedes terminal output at equal timestamps.

- Defined the V3 production composition-root contract before implementation:
  explicit secret/config validation, one official Feishu long connection,
  durable task and outbox loops, live context reconciliation, restart recovery,
  and a draining shutdown with no Codex/MCP dependency.

- Defined the V3 model/task execution recovery boundary before implementation:
  local stateless Responses API usage, stable run continuation before or after
  the first checkpoint, one leased task per SessionKey, concurrent independent
  sessions, and a terminal failure reply on retry exhaustion.

- Defined the V3 attachment ingest and bounded parser contract before code:
  trusted source-reference authorization, encrypted content-addressed blobs,
  crash-safe reference commits, strict byte/ZIP/output ceilings, inert Office
  and PDF extraction, typed image/AV handling, and blocked active content.

- Defined the V3 IM relationship-first context provider before implementation:
  trusted source bindings, mandatory live trigger refetch, same-chat isolation,
  chronological source-versioned evidence, tombstone exclusion, and explicit
  incomplete-history warnings without stale-cache fallback.

- Defined the service-native IM transport and publisher boundary before
  implementation: pinned official Channel SDK, bounded fast callbacks, explicit
  canonical conversion, source-message/thread replies, and durable outbox
  idempotency with ambiguous-send retry.

- Specified the first V3 Feishu IM implementation boundary before code: official
  Channel SDK transport, Codex2Lark-owned normalized values, explicit bot
  mention matching, encrypted typed mirror storage, and replay-safe admission
  with an acknowledgement outbox intent.

- Defined the Runtime API 1 multi-Agent persistence and transaction boundaries
  before implementation: atomic graph/root creation and child spawn, scoped
  tools/budgets/context, acyclic dependencies, durable mailboxes and artifacts,
  resource write locks, lease recovery, cancellation cascade, and root-only
  terminal publication.

- Defined the Runtime API 1 one-node Harness values before implementation:
  immutable model/tool/result/outcome boundaries, monotonic encrypted RunEvents,
  complete-turn checkpoints, hard budgets, semantic tool policy, and verified
  external-effect completion.

- Selected the Phase 1 storage primitives before implementation: serialized
  SQLite access, WAL transactions, durable leases/idempotency/outbox, owner-only
  data paths, and versioned AES-256-GCM envelope encryption with an externally
  provided base64 32-byte master key.
- Refined serialized SQLite ownership after implementation validation: one
  dedicated database Actor thread owns the live connection and communicates
  through bounded request/result queues, avoiding both event-loop blocking and
  cross-thread connection movement.

- Established the V3 clean-redesign contract before implementation: existing
  code and internal behavior may be replaced without compatibility shims.
  Reframed Codex2Lark as a single-node general Feishu Agent Runtime using
  Harness Engineering, a Codex-inspired rooted multi-Agent graph, and Pi-inspired
  small sessions, progressive resources, lifecycle events, and compaction.
- Defined durable multi-Agent delegation, scoped context and tools, typed
  mailboxes/artifacts, resource locking, cancellation, recovery, root-only
  terminal publishing, and collaboration evals in `multi-agent-runtime.md`.
- Defined trusted capability plugins, encrypted SQLite/blob persistence,
  retention and reconciliation, the Feishu IM plugin, explicit design choices,
  V3 package boundaries, and phase exit gates before any runtime refactor.

- Defined the `Codex2Lark` product name and lowercase `codex2lark` machine
  identifier contract before renaming code, packaging, plugin metadata, Skill
  dependencies, tests, caches, and documentation.
- Defined the original stateless product scope in `requirements.md` before
  project code; the later V3 entries above supersede it for the target runtime.
- Defined the Skill + MCP + ephemeral compiler architecture in
  `architecture.md` before project code.
- Defined the first-release MCP surface in `mcp-tools.md` before tool
  implementation.
- Defined the supported authoring/editing behavior in `document-authoring.md`
  before compiler and service implementation.
- Defined `uv`, source layout, testing, and validation requirements in
  `development.md` before Python project initialization.
- Added the typed `DocumentSpec` and `feishu_docs_publish` contracts before
  implementing structured rich-document compilation.
- Changed default adapter contract tests to a deterministic asyncio subprocess
  double; real executable/authentication checks remain in `doctor` and opt-in
  integration tests.
- Documented installation, authentication, direct MCP operation, Codex plugin
  wiring, smoke tests, and failure recovery before final runtime packaging.
- Required MCP read/write/destructive annotations before adding them to tool
  registration.
- Documented the ephemeral uv cache used by the bundled MCP child process before
  adding it to runtime configuration.
- Added enforced ambiguity, exact-match, per-operation live snapshot, and block
  ID validation rules before tightening the edit service.
- Documented the temporary MCP SDK settings-model compatibility shim before
  removing its noisy startup warning.
- Defined the bare `lark-cli auth status` response contract and usable-identity
  health rule before correcting adapter normalization and `doctor` reporting.
- Pinned the supported external `@larksuite/cli` runtime to `1.0.89` in the
  installation contract before adding adapter and `doctor` version enforcement.
- Defined the live `Codex2Lark` managed-folder policy, exact title discovery,
  and post-verification bot notification contract before implementing Drive
  resolution, title-targeted editing, and edit completion messages.
- Defined the bounded group-chat digest contract before implementing exact chat
  discovery, chronological sender-aware normalization, selective ephemeral image
  handling, filename-only file entries, managed-folder creation, and read-back
  verification.
- Required bot-visible digest groups to contain the current authenticated user
  before implementing live member inspection, bot invitation, and user-side
  read-back verification in the group-chat membership gate; separated chat
  access identity from user-owned Drive and Docs authoring identity.
- Changed user invitation from digest-time-only to an MCP-lifespan real-time
  `im.chat.member.bot.added_v1` consumer before implementing fixed-key event
  supervision, ready-marker startup, graceful shutdown, bounded restart, and
  the retained digest-time recovery gate.
- Clarified the deployed bot-added event runbook after live verification:
  publishing a Feishu application version is required, the bounded event probe
  must report both readiness and WebSocket connection, and offline or existing
  group membership does not produce a replayed event.
- Reclassified the MCP-lifespan event consumer as a development bridge after
  identifying that stdio MCP cannot provide inbound availability when Codex is
  stopped. Defined the V2 standalone Gateway, reliable broker, per-group router,
  deterministic workers, and N-group isolation before implementation.
- Defined Codex2Lark as a Harness-centered AI Agent after researching OpenAI
  Harness Engineering, Codex Core/App Server and Agent loop, Pi Agent Core,
  LangBot/AstrBot, RabbitMQ, Temporal, and LangGraph. Added the versioned Agent
  loop, message transformation, Resources, Sessions, tool policy hooks,
  steering/follow-up, compaction, verification, eval, and terminal-outcome
  contracts before implementation.
- Replaced the broker-first/Webhook-first V2 event contract with the approved
  lightweight default before refactoring runtime code: one independently
  operated outbound long-connection Gateway, a bounded in-memory `TaskQueue`,
  partitioned per-chat dispatch, and deterministic handlers. RabbitMQ or a
  managed queue is now an optional adapter for restart-safe delivery; Webhook is
  an optional ingress adapter, and MCP no longer owns event availability.
- Defined the responsibility-based Python package layout before moving code:
  shared contracts in `core`, lark-cli integration in `adapters`, compilation
  and verification in `authoring`, application use cases in `services`, event
  runtime in `realtime`, and MCP composition in `interfaces`. Removed the former
  flat internal module paths instead of retaining redundant forwarding shims.
