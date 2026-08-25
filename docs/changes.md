# Documentation-driven change log

This file records behavioral implementation work and the document that authorized
it. It is not a release changelog.

- Defined independent Drive, Sheets, Base, and Whiteboard capability boundaries
  before implementation. The aggregate `feishu-artifacts` plugin is removed;
  each domain owns one manifest/lifecycle and narrow public service port, while
  policy checks explicit Drive dependencies without exposing a generic Drive
  tool. Implementation area: capability packages, production composition/tool
  ownership, authoring service protocols, isolation tests, and package-boundary
  assertions.

- Corrected and defined Feishu event-source supervision before implementation.
  The pinned transport owns the server-authoritative bounded reconnect schedule;
  Codex2Lark observes reconnect transitions, closes admission readiness while
  disconnected, restores it only after confirmation, and publishes content-safe
  dynamic `ready`/`degraded` process health without competing for the socket.
  Implementation area: Channel source health state machine, Gateway/CLI health
  propagation, process status schema, operations runbook, and transition tests.

- Defined root and cross-graph write locking before implementation. Every root
  write resolves its canonical live target after policy/approval, atomically
  acquires the shared tenant-scoped durable resource lock before idempotency or
  external effects, holds it through verification, and releases only that
  target in a cancellation-safe boundary; unscoped delegated writers fail
  closed. Implementation area: ToolExecutor lock lifecycle, Agent graph store,
  production composition, delegation validation, and overlap/isolation tests.

- Defined shared fair Provider/plugin call capacity before implementation. One
  cancellation-safe round-robin gate is shared by root and child Harnesses and
  ToolExecutors; Provider queue wait is inside wall time, plugin permits begin
  after approval and span reconciliation/execution/verification, and trusted
  composition metadata supplies resource ownership. Implementation area:
  capacity gate, Harness/ToolExecutor boundaries, Gateway configuration/wiring,
  content-safe metrics, and direct fairness/cancellation/integration tests.

- Defined durable hierarchical fair task scheduling before implementation.
  Admission persists trusted tenant/application/group scopes; one SQLite
  transaction incrementally enforces global/tenant/app/group caps plus
  SessionKey serialization and advances durable least-recently-served lane
  cursors before selecting the next task. Implementation area: migration,
  scheduling values, runtime admission/store/worker/configuration, maintenance,
  and direct saturation/fairness/recovery tests.

- Defined bounded cooperative Gateway shutdown before implementation. Intake
  stops first, the active batch receives a configurable drain window, cancelled
  tasks atomically return their lease without consuming a retry, and source,
  plugin, and database lifecycle awaits are bounded. Implementation area:
  Gateway config/lifecycle, durable worker cancellation, operations, and direct
  cancellation/drain tests.

- Defined plugin failure isolation before implementation. Production marks IM
  ingress mandatory and business capabilities optional; optional startup/health
  failure keeps the Gateway available, while policy rechecks trusted owning-
  plugin health before every tool call and denies only that capability.
  Implementation area: PluginManager lifecycle, Gateway tool ownership/policy,
  and isolation/recovery tests.

- Defined complete checkpoint invalidation before implementation. Checkpoints
  bind policy version and the canonical allowed-tool schema fingerprint in
  addition to existing Agent/resource/source/compactor versions; retention GC
  removes checkpoints derived from expiring messages before deleting sources.
  Implementation area: tool registry fingerprinting, Harness validation,
  encrypted checkpoint serialization, storage GC, and recovery/retention tests.

- Defined group-bound capability authority before implementation. Runtime chat
  digest calls derive their group exclusively from trusted `ToolContext`, remove
  model-selected chat fields from the schema, and reject missing/cross-group
  delegated targets before service access; explicit interactive MCP targeting
  remains separate. Implementation area: chat-digest semantic tool and direct
  root/delegation authority tests.

- Defined truthful production model costing before implementation. Gateway
  configuration requires positive operator-maintained input/output prices in
  micro-USD per million tokens, the Responses adapter rounds provider usage
  upward, and every production root definition declares configurable wall-time
  and monetary ceilings. Implementation area: Gateway config/composition,
  OpenAI Responses usage adapter, operations runbook, and direct tests.

- Defined enforceable active wall-time and truthful external-effect completion
  before implementation. The Harness persists cumulative monotonic execution,
  bounds provider/tool awaits, and fails deterministically on exhaustion; every
  write/destructive result lacking read-back verification is durably unresolved
  and model prose cannot convert it into completion. Implementation area:
  Harness checkpoints, encrypted checkpoint serialization, outcome gate, and
  direct timeout/write-verification tests.

- Defined trusted IM admission authorization before implementation. The default
  remains bot-present groups plus human actors, with optional exact chat/actor
  allowlists; disabled/revoked chat state overrides configuration, and denial
  occurs before any mirror, ACK, task, rollout, or model side effect.
  Implementation area: Gateway config, admission policy, IM repository state
  probe, production composition, and pre-side-effect tests.

- Reopened the V3 completion claim after a requirement-by-requirement audit.
  Added `v3-completion-audit.md` with concrete contradictions and required
  evidence for admission authorization, hierarchical limits, wall-time and
  verification enforcement, root locks, cross-group authority, plugin
  isolation, checkpoint invalidation, bounded shutdown/source supervision,
  capability boundaries, and readiness/trace coverage. Implementation area:
  the remaining V3 workstreams listed in that audit.

- Completed the V3 implementation audit. The roadmap now records delivered IM,
  authoring, and operations paths while separating shipped contract completion
  from tenant-specific live release evidence. The trusted internal lark-cli
  authoring transport and richer post-V3 editors are classified as deferred
  transport/capability expansion rather than open V3 behavior.

- Defined the opt-in live multi-group release gate before implementation. Given
  two or more exact chat IDs and a start timestamp, a content-safe observer
  verifies completed graph/task state, sent acknowledgement, and exactly one
  sent terminal result per group without decrypting payloads. Implementation
  area: acceptance CLI, read-only lifecycle query, timeout behavior, and tests.

- Defined an executable encrypted-task backup/restore recovery rehearsal before
  implementation. A restored database opened with the same external key must
  lease and complete previously pending work under a new Worker identity.
  Implementation area: storage-maintenance recovery acceptance and operator
  rehearsal runbook.

- Defined the exact-title group edit vertical acceptance before implementation.
  The production Gateway path must search, inspect, edit, read back, and publish
  one verified terminal result; ambiguity never reaches mutation. Implementation
  area: full-composition acceptance fixture using the existing document tools.

- Defined deterministic single-node root-Agent canary rollout before
  implementation. Trusted group bindings select a stable or canary definition
  at durable admission; retry/restart preserve the stored version, and zero
  percent rolls new work back without changing in-flight tasks. Implementation
  area: Gateway configuration/readiness, rollout selector, IM admission task
  binding, definition dispatch, and restart/stickiness tests.

- Defined the content-safe single-node metrics snapshot before implementation.
  `storage status` adds lifecycle state counts, aggregate retries, and oldest
  pending queue ages without decrypting any business payload. Implementation
  area: storage status model/query, CLI JSON contract, and empty/populated-store
  tests.

- Defined durable group-history publishing before implementation. The V3 root
  Harness receives a strict `feishu.chat.digest.publish` workflow tool backed by
  the existing chronological chat service, trusted identity binding, logical
  per-chat write serialization, managed-folder placement, and live document
  verification. Implementation area: chat-digest capability plugin, Gateway
  composition, tool contracts, and production-registry acceptance.

- Defined the group-to-professional-document vertical acceptance before
  implementation. The production Gateway composition may receive a narrow
  authoring service bundle for deterministic testing, while the full mention,
  Harness, semantic-tool, verification, and IM outbox path remains unchanged.
  The fixture requires a native table, Mermaid whiteboard, managed-folder
  evidence, verified URL, and one explicit terminal reply. Implementation area:
  Gateway composition boundary and end-to-end acceptance test.

- Defined the production root-to-child mailbox surface before implementation.
  `agent.message` durably and idempotently sends bounded message/steer/follow-up
  updates to not-yet-running direct children, the delegation barrier delivers
  them before lease, child Harnesses consume them as non-authority user-level
  task updates, and `agent.status` exposes content-free lifecycle summaries.
  Implementation area: mailbox migration/store idempotency, delegation tools,
  child context construction, production registry, and recovery tests.

- Defined capability target resolution and logical create reservations before
  implementation. Managed document/Sheet/Base creates use normalized title
  digests, existing Sheet/Base/Whiteboard operations use typed resource locks,
  and execution re-resolves actual arguments to reject false declarations.
  Implementation area: shared target utility, semantic authoring tools,
  delegation concurrency tests, and production capability registry.

- Defined broad local business-data purge before implementation. Stopped-Gateway
  `purge-tenant` spans all tenant apps and derived runtime state; `purge-all`
  removes every local business row and encrypted blob while preserving schema
  migrations and exactly one content-free audit record. Implementation area:
  storage maintenance, CLI, operator docs, and destructive-scope tests.

- Defined observable complete-turn compaction before implementation. Context
  builds report journal compaction, the Harness emits a content-free
  `context_compacted` lifecycle event, and overflow evals reject assistant/tool
  pair splitting. Implementation area: context build metadata, Harness event
  stream, and compaction tests.

- Defined the single-node multi-Agent restart-chaos and 64-group burst
  acceptance fixtures before implementation. The fixtures cover three expired
  child leases, encrypted checkpoints, durable mailbox redelivery, lock expiry,
  concurrent artifact recovery, root-only termination, per-SessionKey
  serialization, and bounded progress across independent groups. Implementation
  area: multi-Agent and task-scheduler acceptance tests.

- Defined production progress and destructive-tool approvals before
  implementation: one retry-safe execution-start notice, stable argument-free
  approval identities, encrypted approval cards, no-attempt task deferral,
  requester-only durable card decisions, idempotent conflicting-click handling,
  expiry, and exact resume semantics. Implementation area: runtime schema/store,
  tool policy/broker, task scheduler, Channel card callback bridge, IM publisher,
  production composition, and approval/progress recovery tests.

- Defined direct single-node Gateway process control before implementation:
  foreground/run compatibility, daemon start readiness waiting, content-safe
  PID/status/log files, exact process validation before SIGTERM, bounded graceful
  stop, stale-state refusal, and separation from systemd/Docker ownership.
  Implementation area: CLI parser, Gateway signal lifecycle, process controller,
  usage/operations docs, and subprocess lifecycle tests.

- Defined resumable wrapping-key rotation before implementation: stopped-
  Gateway exclusive ownership, current/new key separation, a crash marker,
  mixed-envelope repeatability, database/blob verification, startup refusal
  during incomplete rotation, and explicit operator key-retirement order.
  Implementation area: envelope cipher, rotation catalog/service, Gateway
  preflight, storage CLI, and interruption/recovery tests.

- Defined single-node disk protection and exact local purge before
  implementation: validated capacity ceilings, a pre-download hard gate,
  metadata-only attachment fallback, content-safe pressure status, and
  stopped-Gateway message/chat purge covering derived runtime payloads and
  unreferenced blobs. Implementation area: Gateway configuration, capacity
  monitor, attachment loader, storage maintenance/CLI, and pressure/purge tests.

- Defined authoritative IM invalidation before implementation: durable
  pre-ACK recall and bot-removal admission, monotonic tombstones, immediate chat
  access revocation, reference-safe attachment cleanup, indexed checkpoint
  invalidation, and live edit reconciliation because Feishu has no ordinary
  message-edit push event. Implementation area: Channel adapter, IM lifecycle
  tasks/repository, session source index, context recovery, storage migration,
  and end-to-end lifecycle tests.

- Defined trusted parallel-writer delegation before implementation: structured
  targets, capability-owned live resolution, created-before-ready activation,
  durable multi-target lock acquisition, crash recovery, per-child persisted
  write scopes, exact call-target enforcement, and conservative serialization
  for unresolved/create targets. Implementation area: delegation schema,
  supervisor/store lifecycle, Docs target resolver, ToolExecutor scope gate,
  production composition, and concurrency/recovery tests.

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
