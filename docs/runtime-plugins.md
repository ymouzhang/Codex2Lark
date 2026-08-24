# Codex2Lark Runtime and capability plugin contract

## 1. Decision

Codex2Lark is a general Feishu Agent Runtime with trusted, typed capability
plugins. Instant messaging is the first plugin, not the platform architecture.
Calendar, Task, Approval, Meeting, Mail, Docs, Sheets, Base, and future Feishu
domains must join through the same stable contracts without modifying the Agent
loop, event scheduler, identity boundary, or storage kernel.

The design borrows Pi's small Agent core, replaceable sessions, progressive
resource loading, and extension composition. It intentionally rejects
unrestricted runtime hooks: Feishu plugins can perform business writes and
handle sensitive data, so every extension point is typed, declared, policy
checked, observable, and testable.

## 2. Runtime kernel

The kernel owns only cross-domain behavior:

```text
RuntimeKernel
├── PluginManager
├── EventRuntime
├── DurableScheduler
├── AgentHarness
├── ContextEngine
├── ResourceLoader
├── IdentityBroker
├── PolicyEngine
├── StorageEngine
├── ResultRouter
└── Observability
```

The kernel does not know how to parse an IM post, create a calendar event,
approve an instance, edit a document, or download a meeting transcript. Those
behaviors belong to capability plugins.

## 3. Plugin kinds

### 3.1 Capability plugin

A capability plugin is trusted Python code that connects one bounded Feishu
domain to Runtime ports. It may contribute:

- event declarations and event normalizers;
- deterministic event handlers;
- Agent trigger policies;
- context providers;
- semantic tools and verifiers;
- result publishers;
- typed repositories and migrations;
- health checks and redacted metrics.

Examples are `feishu-im`, `feishu-docs`, `feishu-calendar`, and
`feishu-approval`.

### 3.2 Resource package

A resource package is declarative and cannot execute code. It may contain:

```text
resources/
├── skills/
├── prompts/
├── policies/
├── response-templates/
├── context-rules/
└── evals/
```

Resource loading is progressive. The Harness starts with a small stable
AgentDefinition and loads only the resources required by the admitted request.
Tone, locale, and response wording are resources rather than event-handler code.

## 4. Manifest

Every plugin has a versioned manifest validated before code activation:

```yaml
id: feishu-im
version: 1.1.0
runtime_api: 1
entrypoint: codex2lark.capabilities.im.plugin:create_plugin
capabilities:
  - im.group_message.receive
  - im.message.reply
  - im.thread.read
  - im.attachment.read
events:
  - im.message.receive_v1
  - im.message.recalled_v1
  - im.chat.member.bot.added_v1
  - im.chat.member.bot.deleted_v1
required_scopes:
  - im:message:readonly
  - im:message:send_as_bot
storage_namespace: im
resources:
  - resources/im
```

Required manifest fields are:

| Field | Contract |
|---|---|
| `id` | Stable lowercase identifier; globally unique |
| `version` | Plugin behavior and migration version |
| `runtime_api` | Required kernel compatibility version |
| `entrypoint` | Explicit trusted factory; no directory scanning and execution |
| `capabilities` | Semantic capability IDs exported by the plugin |
| `events` | Fixed Feishu event keys the plugin may subscribe to |
| `required_scopes` | Scopes verified by health checks and operations guidance |
| `storage_namespace` | Unique prefix for plugin-owned tables and blobs |
| `resources` | Declarative resource roots available to ResourceLoader |

The manifest is configuration and discovery metadata, not authority. Runtime
policy may disable any declared event, tool, identity, or resource.

## 5. Stable plugin API

The first Runtime API exposes these ports:

```python
class CapabilityPlugin(Protocol):
    manifest: PluginManifest

    def event_adapters(self) -> Sequence[EventAdapter]: ...
    def deterministic_handlers(self) -> Sequence[DeterministicHandler]: ...
    def trigger_policies(self) -> Sequence[TriggerPolicy]: ...
    def context_providers(self) -> Sequence[ContextProvider]: ...
    def semantic_tools(self) -> Sequence[SemanticTool]: ...
    def result_publishers(self) -> Sequence[ResultPublisher]: ...
    def repositories(self) -> Sequence[RepositoryFactory]: ...
    def migrations(self) -> Sequence[Migration]: ...
    async def health(self) -> PluginHealth: ...
```

Plugins depend on Runtime ports and domain-neutral core types. The kernel never
imports a concrete plugin implementation. One plugin never imports another
plugin's private modules, repositories, or tables.

Cross-capability work is orchestrated through the Harness:

```text
IM request
  -> Agent Harness
  -> docs.create semantic tool
  -> Docs plugin verifies result
  -> IM result publisher replies to source message
```

## 6. Events

An `EventAdapter` maps one declared upstream event into a typed normalized
event. The normalized envelope contains trusted routing metadata and a payload
reference:

```text
NormalizedEvent
├── event_id
├── event_type
├── tenant_key
├── app_id
├── occurred_at
├── received_at
├── principal_ref
├── resource_ref
├── correlation_ref
├── payload_policy
└── trace_id
```

Domain-specific identifiers live inside typed references. IM may add chat,
thread, message, and sender references; Approval may add definition, instance,
and task references. The kernel does not add every future Feishu identifier to
one universal event class.

An event produces one of three commands:

- deterministic command: execute without a model;
- Agent run request: pass through admission and Harness;
- ignored event: record a reason without downstream execution.

Raw events are never direct model input. A plugin may retain an encrypted raw
payload for a bounded replay/audit TTL when live refetch cannot reconstruct the
event. The manifest and retention policy declare this behavior.

## 7. Trigger and admission policy

Trigger policies are deterministic and run before model inference. They bind:

- tenant, application, and source resource;
- authenticated sender or actor;
- AgentDefinition and enabled plugin set;
- credential reference and execution identity;
- allowed tool profile and approval policy;
- SessionKey and concurrency class;
- acknowledgement and terminal-result channel.

The model cannot override those bindings. A plugin may extract a user request,
but it cannot decide its own authority.

## 8. Context providers

A `ContextProvider` supplies bounded, source-attributed evidence. It declares:

```text
provider id
supported resource kinds
input reference schema
output content kinds
cost estimate
sensitivity
cache and retention policy
invalidation signals
hard item and total limits
```

Providers are invoked progressively by ContextEngine. They do not append to the
model prompt directly. ContextEngine applies authorization, token limits,
deduplication, provenance labels, prompt-injection boundaries, ordering, and
compaction.

Examples:

- IM: trigger message, reply chain, thread, recent chat, attachment;
- Calendar: event detail, attendees, availability;
- Approval: definition, instance, task history;
- Meeting: meeting metadata, transcript segment, minutes;
- Docs: selected document blocks and revision.

## 9. Semantic tools

A semantic tool describes user-level intent, not raw OpenAPI or lark-cli
arguments. Each tool declares:

- strict input and output schemas;
- capability and plugin identity;
- read/write/destructive classification;
- required trusted bindings;
- approval rule;
- idempotency strategy;
- verification strategy;
- timeout, retry, and observation limits.

Tool execution order is:

```text
validate
  -> bind trusted target and identity
  -> authorize
  -> obtain approval when required
  -> execute adapter
  -> read back observable state
  -> verify
  -> emit typed observation
```

No plugin exposes arbitrary shell execution, unrestricted SQL, raw lark-cli,
or a generic OpenAPI path to the model.

The first authoring profile exposes these bounded semantic document tools:

| Tool | Effect | Contract |
|---|---|---|
| `feishu.docs.search` | Read | Resolve exact titles in the managed folder and reject ambiguity before mutation |
| `feishu.docs.inspect` | Read | Fetch live XML and revision; its body is non-persistable Harness evidence |
| `feishu.docs.create` | Write | Create XML in the managed folder and read it back against required text/title |
| `feishu.docs.edit` | Write | Resolve one URL/token or exact title, apply one bounded edit, read back, and notify |

These tools may use the existing argument-array `lark-cli` adapter internally
as a transitional trusted adapter. The model sees only strict schemas and
semantic results. The adapter identity is configured by the operator, is bound
outside model input, and cannot be overridden in tool arguments.

The authoring artifact plugin adds bounded `feishu.whiteboard.render`,
`feishu.sheets.create`, `feishu.sheets.write`, `feishu.base.create`, and
`feishu.base.upsert` tools. Each schema bounds collection sizes through its
validated request model. Every write returns an upstream confirmation or live
read-back verification record; Sheet writes specifically read values/formulas
back and reject formula errors. Artifact observations are non-persistable
because cells, records, and diagram sources may contain business content.

The `feishu-chat-digest` workflow plugin exposes
`feishu.chat.digest.publish` to the durable group Harness. It reuses the same
typed chat-history service as interactive MCP: one exact chat ID or exact group
name, one bounded time range, chronological sender-attributed output, permitted
image insertion, filename-only file representation, managed-folder placement,
and live document verification. Chat identity is bound by trusted composition
and is never model supplied. Concurrent digest writes for the same normalized
chat target are serialized through a logical write reservation. The plugin is a
bounded workflow capability; it does not expose raw history queries or lark-cli.

## 10. Result publishers

Result publishers render Harness events and terminal outcomes into a channel.
They may publish acknowledgements, progress, approval requests, completion,
blocked, failed, and cancelled results. They receive a bounded result model,
not the complete model transcript.

The IM publisher replies to the source message or thread. Future publishers may
update an approval card, a task record, a document comment, or an administrative
UI. Channel-specific formatting never changes terminal-state semantics.

## 11. AgentDefinition composition

Plugins define what the installation can do. An immutable AgentDefinition
selects what one Agent profile may do:

```yaml
id: codex2lark-default
version: 1
plugins:
  - feishu-im
  - feishu-docs
  - feishu-sheets
tools:
  allow:
    - im.message.reply
    - docs.inspect
    - docs.create
    - docs.edit
approvals:
  destructive_write: required
context:
  providers:
    - im.trigger
    - im.thread
    - im.recent_chat
    - im.attachment
```

The control plane binds a Feishu group, user, or workflow to one exact
AgentDefinition version. Updating a plugin or resource package does not silently
change an existing AgentDefinition; rollout is explicit and evaluable.

The single-node Gateway supports one stable root definition and at most one
canary root definition. Admission deterministically hashes trusted tenant, app,
and chat identifiers with an operator salt into 100 buckets, stores the selected
definition version in the encrypted task payload, and never reselects it during
retry or restart. The rollout percentage controls only newly admitted tasks.
Setting it to zero is immediate rollback for new work; the canary definition
must remain configured until its already-admitted tasks drain. Canary and stable
definitions use the same policy/tool boundary but may select different model
profiles and immutable definition versions. A canary percentage above zero
without a distinct positive version and non-empty salt fails readiness.

Production AgentDefinitions do not assemble policy and tone from ad hoc strings
in the composition root. Codex2Lark ships immutable JSON resource packages
inside the Python distribution. Startup strictly validates package ID, semantic
version, instructions, policies, and response-template strings; duplicate IDs,
unknown fields, empty required fields, or missing selected packages fail
readiness. The root and delegated-worker definitions select exact package IDs.
Harness checkpoints record the resolved versions and refuse resume after an
incompatible package change.

The IM acknowledgement and terminal suffix bundle is likewise versioned package
data. It is loaded once at composition and mapped to the typed response-template
object; event content and model output cannot select a different locale or
override these trusted strings. The first production profile ships
`group-agent-core@1.1.0`, `delegated-worker-core@1.0.0`, and `im-zh-CN@2`.
The group package teaches the root to include exact structured targets only for
existing resources that a capability can resolve live; it keeps create and
unresolved writer delegation serial.

## 12. Discovery and trust

The first release supports:

1. built-in capability plugins shipped with Codex2Lark;
2. installed Python packages explicitly named in operator configuration and
   allowlisted by plugin ID and entrypoint.

The first release does not support:

- installation requested by a Feishu message;
- model-generated executable plugins;
- arbitrary directory scanning and import;
- untrusted hot reload;
- plugins that monkey-patch the kernel or other plugins.

Future distribution may use Python entry-point metadata and signed packages,
but installation and activation remain operator actions.

## 13. Lifecycle

Plugin lifecycle is:

```text
discover
  -> validate manifest
  -> resolve dependency-free capability IDs
  -> validate migrations
  -> migrate
  -> initialize
  -> start event sources and health checks
  -> ready
  -> drain
  -> stop
```

`ready` means manifest, scopes, storage, resources, and mandatory adapters are
healthy. A plugin that fails validation never runs migrations or event sources.
Migration failure stops that plugin and prevents dependent AgentDefinitions from
admission.

## 14. Failure isolation

A plugin has its own health and circuit state. Failure behavior is explicit:

- a failed event source affects only its event declarations;
- a failed context provider returns a typed unavailable/truncated result;
- a failed optional provider may degrade with a warning;
- a failed required provider blocks the run;
- a tool failure produces a typed observation and never bypasses verification;
- a plugin marked unhealthy admits no new work that requires it;
- queued work remains recoverable and other plugins continue.

Plugins share one process in the single-node profile, so memory corruption or a
fatal interpreter failure remains a common failure domain. Process isolation is
deferred until evidence justifies the operational cost.

## 15. Storage ownership and migrations

The kernel owns only cross-domain tables:

```text
runtime_plugins
runtime_migrations
runtime_events
runtime_tasks
runtime_runs
runtime_run_events
runtime_idempotency
runtime_outbox
```

Plugins own typed namespaced tables such as:

```text
im_messages
im_threads
im_attachments
calendar_events
calendar_attendees
approval_instances
approval_tasks
```

A universal `business_objects(json)` or EAV schema is forbidden. JSON may hold
versioned upstream payload fragments when the exact shape is inherently
extensible, but identity, lifecycle, authorization, query, retention, and
foreign-key fields remain typed columns.

Migration IDs are `(plugin_id, plugin_version, sequence)`. Migrations are
forward-only in production, transactional when SQLite permits, checksummed, and
recorded before a plugin becomes ready. Downgrade requires restoring a matched
database and encrypted blob backup.

## 16. Runtime API evolution

V3 does not preserve compatibility with V2 implementation APIs. After the V3
Runtime API is released, its own evolution follows these rules:

- additive optional manifest fields are backward compatible;
- removing or changing a port method requires a new `runtime_api` major version;
- tool schema changes require a new tool version or capability ID;
- stored context and events include schema versions;
- plugin migrations cannot modify another namespace;
- an AgentDefinition remains loadable only while its referenced Runtime API,
  plugins, resources, and migrations are inside the declared support window;
- a startup compatibility report lists accepted, disabled, and rejected plugins.

## 17. Tests and evaluation

Every plugin supplies:

- manifest and compatibility tests;
- migration tests from an empty and previous supported schema;
- deterministic event normalization fixtures;
- trigger and authorization tests;
- context budget, provenance, and injection tests;
- semantic tool contract and verification tests;
- duplicate delivery and idempotency tests;
- health, degradation, restart, and retention tests;
- Agent eval fixtures for intended and prohibited behavior.

The kernel test suite runs against fake plugins so it does not depend on any
Feishu domain. Capability suites run against fake Runtime ports and recorded
Feishu adapter envelopes. Live tests remain explicit and disposable.
