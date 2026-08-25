# V3 completion audit

This document is the authoritative requirement-to-evidence audit for declaring
Codex2Lark V3 complete. A green unit suite is necessary but not sufficient: each
normative MUST and acceptance criterion needs a production-path implementation
and a direct executable proof.

## Audit status

The durable kernel, Harness loop, multi-Agent graph, IM transport, authoring
tools, encrypted storage, and operator controls satisfy the audited V3
contracts. Every gap found by the requirement-by-requirement audit now has a
production-path implementation and direct executable proof. Credential-bound
live-environment qualification remains an operator release activity, not an
unimplemented architecture contract.

| Gap | Normative source | Current contradictory evidence | Required completion evidence |
| --- | --- | --- | --- |
| Admission enablement and actor authorization — resolved | Requirements 4.1 and acceptance 1/12 | Production now applies exact optional chat/actor allowlists and persisted chat enablement/access state before any mirror or durable side effect | `test_admission_policy_rejects_chat_and_actor_before_any_side_effect` and `test_persisted_disabled_chat_overrides_default_open_policy` prove the production policy boundary |
| Hierarchical execution limits — resolved | Requirements 4.2 | Production task leasing enforces fair global/tenant/app/group caps; one shared cancellation-safe round-robin gate bounds every root/child Provider call and every plugin's post-approval external operation | Direct durable saturation/fairness/recovery plus call-gate fairness/cancellation/root-child integration tests prove all configured layers |
| Wall-time and production cost budgets — resolved | Requirements 4.3 and acceptance 5 | The Harness persists cumulative active monotonic time and bounds model/tool awaits; the production adapter calculates micro-USD from operator-supplied current prices and every root definition declares wall/cost ceilings | Direct timeout/resume, price calculation, configuration, and production-definition tests prove deterministic enforcement |
| Truthful write completion — resolved | Requirements 4.3/4.8 and acceptance 15 | Every unverified write/destructive result is durably tracked and blocks a completed terminal state, independent of model prose or the definition's minimum-effect flag | Direct failed/uncertain/policy-denied write tests and read-only completion tests prove the outcome gate |
| Root and cross-graph write locking — resolved | Requirements 4.4 and acceptance 7/13 | Production root and delegated writes now resolve canonical live targets, use the same tenant-scoped durable lock table, renew leases through reconciliation/execution/read-back, and release only transient root targets | Direct cross-graph overlap, disjoint-target, lease-renewal, cancellation-release, tenant-binding, and unscoped-child rejection tests prove the write boundary |
| Cross-group tool authority — resolved | Requirements 4.6 and security requirements | The group-Harness digest schema no longer accepts a target; execution and lock resolution inject the trusted current `ToolContext.chat_id`, and delegated cross-chat declarations fail before service access | Direct root/delegated authority tests prove same-chat execution and zero-service-call rejection; interactive MCP remains separately explicit |
| Runtime plugin isolation and readiness — resolved | Requirements 4.6 and acceptance 14 | Production classifies IM ingress as mandatory and business capability plugins as isolated optional providers; tool policy rechecks trusted owning-plugin health immediately before execution | Direct startup isolation, mandatory failure, tool denial, and live recovery tests prove unrelated work remains available |
| Checkpoint invalidation contract — resolved | Requirements 4.5 and Harness contract | Encrypted checkpoints bind Agent/resource/source/compactor/policy versions and a canonical allowed-tool schema fingerprint; retention GC deletes checkpoints derived from expiring message/attachment sources in the same transaction | Direct policy/schema mismatch recovery and retention-expiry tests prove stale tool calls/context are discarded |
| Bounded shutdown and source supervision — resolved | Requirements 4.1 and operability | Gateway stops intake first, bounds every lifecycle phase, and defers cancelled leases without retry cost; the Feishu source now observes the pinned transport's server-authoritative bounded reconnect lifecycle, gates all raw/normalized admissions while disconnected, and publishes dynamic content-safe health | Direct cancellation/drain, reconnect admission-gate, recovery, and `ready`/`degraded` process-status tests prove the lifecycle boundary |
| Capability plugin boundaries — resolved | Product definition and maintainability | Drive, Sheets, Base, and Whiteboard now have independent manifests, scope sets, health lifecycles, narrow service ports, and trusted tool ownership; the aggregate `feishu-artifacts` policy/lifecycle no longer exists | Direct manifest/lifecycle, package-boundary, owner-isolation, and Drive-dependency recovery tests prove capability-local failure behavior |
| Readiness and trace coverage — resolved | Non-functional operability/observability | Production startup performs a metadata-only model probe and requires the Feishu Channel to be connected with a resolved bot identity before publishing `ready`; one opaque event trace is inherited by the task lease, run, Agent graph/nodes, run/tool events, approvals, and task-bound outbox relations | Provider-probe failure-before-source, strict process-readiness, bot-identity startup, reconnect health, and end-to-end content-safe database-join tests prove the boundary |

## Already proven

The audit retains the existing direct evidence for SQLite/WAL admission,
encryption, idempotent outbox, targeted purge, backup/restore/key rotation,
complete-turn compaction, durable controls and approvals, three-worker restart
recovery, 64-group fairness, exact target locking for delegated writers,
professional document create/edit workflows, group digest rendering, canary
selection, metrics, process control, and the opt-in live multi-group observer.

## Completion rule

Each row above is implemented documentation-first and committed as a key
feature. Repository completion additionally requires canonical validation,
wheel/sdist construction, Codex plugin-bundle validation, and an offline
Gateway composition check. Credential-dependent model/Feishu live checks are
recorded separately in the release runbook and must pass in each deployment
environment before rollout.
