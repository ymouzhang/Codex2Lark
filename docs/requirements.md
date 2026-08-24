# Codex2Lark V3 product requirements

## 1. Problem

People collaborate in many Feishu groups and expect an Agent to understand an
addressed request, collect authorized conversation and file context, coordinate
specialized work, operate Feishu resources, and report a verified result. A
Codex or ChatGPT conversation may also invoke the same capabilities through MCP.

An interactive AI client is not an always-on event service. A single large
prompt is not a safe orchestration system. The product therefore needs a
durable, observable Agent Harness that serves many groups and users concurrently
while preserving identity, authorization, context, and target isolation.

## 2. Product definition

`Codex2Lark` is a single-node Feishu Agent Runtime with:

- an always-on outbound long-connection event service;
- a durable SQLite scheduler and transactional result outbox;
- a rooted multi-Agent Harness with bounded delegation and recovery;
- progressively loaded Skills, prompts, policies, templates, and evals;
- trusted typed plugins for IM, Drive, Docs, Sheets, Base, Whiteboard, identity,
  and future Feishu domains;
- semantic MCP tools for active Codex/ChatGPT clients;
- an authorized encrypted local mirror of selected messages and files;
- live authorization checks and read-back verification for external effects.

Feishu is the upstream source of truth. Local content is a policy-controlled
mirror for recovery, context, parsing, and performance, not a second document
system.

### Naming

- Human-facing name: `Codex2Lark`.
- Distribution, package, CLI, MCP server, plugin, cache, and temporary prefix:
  `codex2lark`.
- The former identifier is not retained as an alias.

### Compatibility

V3 is a clean redesign. Existing internal modules, interfaces, runtime behavior,
and absence of a database may be replaced. No compatibility shim is required.
Migration work is limited to explicitly retained operator configuration and
approved business data.

## 3. Primary use cases

### Group Agent request

In any enabled group, an authorized user mentions the bot with a non-empty
request. The runtime durably admits it, responds promptly in a gentle and concise
tone, gathers bounded group/thread/file context, runs the required Agent graph,
and posts one explicit verified terminal result.

### Conversation to professional document

The Agent turns a discussion into a polished Feishu document in the managed
`Codex2Lark` folder. It may create native tables, Mermaid-derived diagrams,
whiteboards, Sheets, Base resources, images, attachments, and links. It reads
the created document back before reporting completion.

### Find and modify an existing resource

When the user names a document rather than providing a token, the Agent searches
authorized Feishu storage, resolves ambiguity explicitly, inspects the live
resource, applies a bounded change, reads it back, and reports what changed.

### Group history digest

The Agent finds an exact group, collects messages for an authorized time range,
orders them by source time and speaker, embeds permitted images, represents
files by name unless their content is explicitly needed, publishes a document
named after the group, and verifies it.

### Multi-artifact collaboration

One root Agent may delegate independent research, document, Sheets/Base, and
verification work to scoped worker Agents. Workers return typed artifacts; the
root integrates them and owns the final reply.

### Future Feishu workflows

Calendar, Task, Approval, Meeting, Mail, Wiki, and other domains join as typed
capability plugins without changing the Harness, scheduler, identity model, or
Agent collaboration protocol.

## 4. Functional requirements

### 4.1 Event and admission

The runtime MUST:

- operate independently of Codex and stdio MCP availability;
- consume fixed Feishu events through an outbound long connection;
- independently supervise event sources and reconnect with bounded backoff;
- durably deduplicate source events before acknowledging work;
- reject bot loops, malformed events, empty mentions, disabled groups, and
  unauthorized actors before model inference;
- bind tenant, app, chat/thread, actor, execution identity, AgentDefinition,
  policy, retention, and budgets outside model-visible content;
- persist acknowledgement intent atomically with task admission.

### 4.2 Multi-group concurrency

The runtime MUST:

- serve N groups and users with one installation;
- serialize one SessionKey while running independent SessionKeys concurrently;
- enforce global, tenant, app, group, plugin, and provider limits;
- schedule fairly so one noisy group cannot starve others;
- preserve source attribution and prevent all cross-tenant/group context leaks;
- recover leased tasks after process restart.

### 4.3 Agent Harness

The Harness MUST:

- execute versioned immutable AgentDefinitions;
- expose normalized thread, turn, model, tool, approval, compaction,
  verification, mailbox, and terminal events;
- load resources progressively;
- enforce token, tool, time, cost, node, depth, and concurrency budgets;
- support steer, follow-up, interrupt, cancel, checkpoint, and resume;
- preserve complete tool-call/result pairs during compaction;
- stop only in `completed`, `blocked`, `failed`, or `cancelled` terminal state;
- require verification evidence for `completed` external-effect tasks.

### 4.4 Multi-Agent collaboration

The supervisor MUST:

- create one root Agent per admitted user task;
- allow only concrete, bounded, policy-authorized child tasks;
- give every node an isolated context, tool allowlist, budget, deadline, and
  durable lifecycle;
- use a rooted tree and acyclic execution dependencies;
- communicate through durable typed mailboxes;
- prevent authority escalation through delegation or messages;
- prevent concurrent writes to overlapping Feishu targets;
- support cancellation cascade and restart recovery;
- allow only the root to publish the task's terminal user outcome.

### 4.5 Context and files

The runtime MUST:

- treat the trigger event as a wake-up reference and fetch authoritative source
  data when freshness requires it;
- collect relationship-first context: trigger, root/reply chain, thread, then
  bounded recent group messages;
- retain sender, source time, mentions, edits, recalls, deletion, and attachment
  provenance;
- treat chats, documents, filenames, links, parsed files, and child artifacts as
  untrusted evidence;
- download bytes only when required and allowed by type/size/retention policy;
- never execute macros, formulas, scripts, archives, or embedded programs;
- produce parser-versioned, hash-bound, attributed, and explicitly truncated
  evidence;
- invalidate checkpoints and parser results after relevant edit, recall,
  deletion, permission loss, policy change, or retention expiry.

### 4.6 Plugins and tools

The runtime MUST:

- load only built-in or explicitly allowlisted trusted capability plugins;
- validate manifest, runtime API, scopes, resources, migrations, and health;
- expose strict versioned semantic tools rather than raw platform operations;
- authorize every tool using trusted bindings;
- separate read, write, destructive, cross-group, and delegated-user policy;
- use stable idempotency keys and capability-specific live verification;
- isolate plugin failure and reject work requiring an unhealthy plugin;
- forbid arbitrary shell, SQL, lark-cli, generic OpenAPI, and model-installed
  executable plugin surfaces.

### 4.7 Persistence and recovery

The single-node profile MUST:

- use SQLite WAL transactions for events, tasks, runs, Agent graphs, leases,
  mailboxes, idempotency, resource locks, and outbox state;
- use typed kernel and plugin-owned schemas rather than universal EAV storage;
- encrypt business content and attachment bytes at rest using an externally
  supplied key;
- keep durable data outside the source repository;
- apply versioned retention and support targeted purge;
- reconcile local mirrors with Feishu edits, recalls, deletion, and access loss;
- protect disk capacity and stop optional downloads before runtime failure;
- support consistent backup and restore of database, encrypted blobs, schema
  manifest, and external key.

### 4.8 User communication

For every admitted group request, the runtime MUST:

- promptly reply that it has received and will handle the request;
- use configurable, gentle, natural, concise language without demographic
  stereotyping or sacrificing accuracy;
- send only factual throttled progress when it materially helps;
- send exactly one root terminal reply stating completion, blockage, failure, or
  cancellation;
- list created/modified resources and relevant verification warnings;
- invite a follow-up question in the completion template;
- never claim completion solely because a model or tool returned success text.

## 5. Non-functional requirements

### Security and privacy

- Credentials never enter prompts, logs, SQLite, blobs, Feishu control records,
  or model-visible tool output.
- Local business data is encrypted, purpose-limited, and deleted by retention.
- Authorization is checked at admission and again before sensitive reads/writes.
- Cross-group access always names and authorizes source and destination.
- Logs and metrics contain lifecycle, identifiers, sizes, hashes, timings, and
  redacted errors, not content or hidden reasoning.

### Reliability

- Committed tasks, Agent graphs, reply intents, and approvals survive restart.
- Duplicate events and retries do not create duplicate logical operations.
- One event source, plugin, group, Agent node, or model failure does not corrupt
  unrelated work.
- The runtime makes no exactly-once claim for external Feishu effects; it uses
  at-least-once execution, idempotency, inspection, and verification.

### Operability

- Production requires one supervised process, one data directory, one external
  encryption key, Feishu credentials, and model credentials.
- It requires no RabbitMQ, Redis, PostgreSQL, object store, public IP, or inbound
  Webhook.
- Startup and readiness report configuration, key, storage, migration, plugin,
  event-source, identity, and provider health without secrets.
- Shutdown drains, checkpoints/releases leases, preserves outbox intent, and
  closes cleanly.

### Maintainability

- Domain logic resides in cohesive capability plugins.
- Kernel tests use fake plugins; plugin tests use fake runtime ports and recorded
  Feishu envelopes.
- Every behavior change begins with docs and ships with tests/evals.
- V3 code follows the target package boundaries in [development.md](development.md).

## 6. Acceptance criteria

V3 is complete only when automated or explicitly opt-in live tests demonstrate:

1. A mention in any enabled group is acknowledged without Codex/MCP running.
2. Multiple groups execute concurrently with per-SessionKey ordering and no
   cross-group context, identity, target, or result leakage.
3. Restart after durable admission recovers the task, Agent graph, mailbox,
   resource locks, and reply intent.
4. One task safely delegates at least three independent workers, integrates
   typed results, and publishes one root terminal reply.
5. Depth, node, concurrency, token, time, tool, and cost budgets stop excessive
   delegation deterministically.
6. User steer/follow-up, approval, interrupt, and cancellation reach the correct
   graph and survive restart.
7. Duplicate delivery/retry produces no duplicate document, edit, invitation,
   or terminal reply.
8. A group request can create and verify a professional document containing a
   correct native table and a rendered architecture diagram in the managed
   folder.
9. An exact-title request finds, disambiguates, modifies, reads back, and reports
   an existing document.
10. Group history is chronological and sender-aware; permitted images are
    embedded and non-required files are represented by filename.
11. Message/file context is encrypted locally, bounded by TTL, invalidated on
    edit/recall/access loss, and removable by targeted purge.
12. Prompt injection in a message, document, file, or child artifact cannot
    change trusted identity, policy, tools, target, or approval.
13. Overlapping concurrent writes are rejected or serialized; disjoint target
    work can run concurrently.
14. Plugin failure blocks only dependent work and produces a truthful terminal
    result.
15. External writes become `completed` only after capability-specific read-back
    verification; unverifiable outcomes are explicit.

Acceptance criterion 8 is exercised through the production composition path,
not by calling the document service directly. Its deterministic fixture admits
an exact bot mention, observes acknowledgement and progress, lets the Harness
invoke `feishu.docs.create`, and supplies Feishu XML containing both a native
`<table>` and a Mermaid `<whiteboard>`. The capability double must inspect the
typed create request, emulate a managed-folder write plus live read-back, and
return a verified document URL. The root then emits exactly one terminal reply
that names the completed document and includes the verified URL. Persisted run
events may contain tool identity and verification state, but never the document
body.
16. Backup/restore returns a compatible encrypted runtime to a recoverable state.
17. The default deployment passes readiness without RabbitMQ, Redis, PostgreSQL,
    public Webhook, or a running Codex task.

## 7. Out of scope for V3

- multi-host high availability and distributed worker scaling;
- an untrusted third-party plugin marketplace or model-installed code;
- a general shell, SQL, filesystem, lark-cli, or arbitrary OpenAPI tool;
- permanent semantic memory unrelated to authorized Feishu sources;
- autonomous cross-group or cross-tenant discovery and writes;
- exactly-once guarantees for upstream Feishu side effects;
- treating local storage as the authoritative business system.
