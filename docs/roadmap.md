# V3 delivery roadmap

V3 is a clean redesign. Existing code is evidence and may be reused when it fits
the new boundaries, but it is not a compatibility constraint. Each phase starts
with contract/eval changes and ends with an executable vertical slice.

## Design gate: accepted before implementation

- Approve [architecture.md](architecture.md),
  [multi-agent-runtime.md](multi-agent-runtime.md),
  [runtime-plugins.md](runtime-plugins.md),
  [single-node-storage.md](single-node-storage.md), and
  [group-agent-runtime.md](group-agent-runtime.md).
- Resolve all contradictions in current/future operation docs.
- Freeze V3 core schemas, terminal semantics, trust boundaries, and initial eval
  fixtures.

Exit: reviewers can trace every product requirement to a component contract,
failure behavior, storage rule, and acceptance test. No V3 code begins earlier.

## Phase 1: Kernel and durable foundation

- Create the V3 package boundaries and one composition root.
- Implement typed IDs, events, errors, clocks, budgets, cancellation, and
  content-safe observations.
- Implement SQLite transactions, migrations, encryption, leases, idempotency,
  outbox, retention primitives, integrity checks, and test fixtures.
- Implement PluginManager with a fake capability plugin and fake event source.
- Add architecture dependency tests and restart/property tests.

Exit: a fake event becomes a durable command, survives restart, executes once
logically, and publishes one idempotent terminal result without Feishu or a
model.

## Phase 2: One-node Harness

- Implement immutable AgentDefinition and role/resource loading.
- Implement SessionStore, ContextEngine, model/tool loop, lifecycle event stream,
  budgets, approvals, steering, follow-up, interruption, checkpoints, and
  complete-turn compaction.
- Implement policy-aware semantic tool registry and VerificationEngine.
- Run the Harness against fake models and fake capability tools.

Exit: a recorded request completes, blocks, fails, and cancels deterministically;
write completion requires verifier evidence and recovery resumes from a valid
checkpoint.

## Phase 3: Multi-Agent supervisor

- Implement rooted task graphs, canonical paths, dependencies, node leases,
  scoped context packages, budgets, and role policies.
- Implement typed durable mailboxes, event-driven wait, steer/follow-up,
  interruption, cancellation cascade, and recovery.
- Implement typed artifacts, resource locks, plan-then-execute, disjoint-target
  concurrency, independent verification, and root merge.
- Add many-graph fairness, depth/breadth/concurrency enforcement, and adversarial
  authority-escalation evals.

Exit: one root safely coordinates at least three workers, restarts mid-graph,
rejects overlapping writes, integrates verified artifacts, and alone publishes
the terminal result.

## Phase 4: Feishu IM vertical slice

- Implement service-native Feishu identity and outbound event adapters.
- Implement IM manifest, event normalization, exact mention admission,
  bot-added membership behavior, acknowledgement, progress, and terminal
  publishers.
- Implement typed chats/messages/attachments repositories, reconciliation,
  retention, purge, and encrypted file ingest.
- Implement relationship-first context providers and bounded parsers.
- Preserve the existing MCP event bridge only until this slice passes live
  acceptance, then remove it instead of maintaining two event runtimes.

Exit: with Codex/MCP stopped, mentions in multiple groups are acknowledged,
processed with isolated persistent context, recovered after restart, and answered
truthfully.

## Phase 5: Authoring capability plugins

- Move Drive discovery/folder policy into `feishu-drive`.
- Move document planning, compile/edit, diagrams, embedded resources, and
  verification into `feishu-docs`.
- Add `feishu-sheets`, `feishu-base`, and `feishu-whiteboard` typed plugins.
- Convert the current authoring Skill into progressive ResourceLoader packages.
- Keep interactive MCP as a thin adapter over the same semantic capabilities.

Exit: a group task and an interactive Codex task use the same capabilities to
create/modify a complex professional document and verify the live result.

## Phase 6: Operations and release hardening

- Implement runtime start/status/stop, readiness, graceful drain, storage status,
  targeted purge, retention GC, backup/restore, and key rotation.
- Add content-safe traces and metrics for sources, lanes, leases, graphs, Agents,
  tools, verification, outbox, plugins, model/provider, and disk pressure.
- Add load, soak, duplicate delivery, process kill, provider outage, Feishu rate
  limit, corruption, backup/restore, and policy rollout tests.
- Add canary AgentDefinition/resource/plugin rollout with eval comparison and
  rollback.
- Rewrite user operations/usage docs to describe only the shipped V3 runtime.

Exit: the single-node service meets the acceptance criteria in
[requirements.md](requirements.md) and has a rehearsed recovery runbook.

## Deferred by evidence, not abstraction

- RabbitMQ or another broker: only for multiple worker hosts or measured SQLite
  queue limits.
- Public Webhook ingress: only when network topology requires inbound callbacks.
- Process-isolated plugins: only when trusted in-process plugins demonstrate a
  containment need.
- Multi-host availability: only after a concrete SLO and storage/leadership
  design exist.
- Untrusted plugin marketplace and durable cross-session semantic memory.
