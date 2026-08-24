# V3 completion audit

This document is the authoritative requirement-to-evidence audit for declaring
Codex2Lark V3 complete. A green unit suite is necessary but not sufficient: each
normative MUST and acceptance criterion needs a production-path implementation
and a direct executable proof.

## Audit status

The durable kernel, Harness loop, multi-Agent graph, IM transport, authoring
tools, encrypted storage, and operator controls are substantial and tested.
The repository is not yet eligible for a V3-complete declaration because the
following normative gaps were found during the requirement-by-requirement audit.

| Gap | Normative source | Current contradictory evidence | Required completion evidence |
| --- | --- | --- | --- |
| Admission enablement and actor authorization — resolved | Requirements 4.1 and acceptance 1/12 | Production now applies exact optional chat/actor allowlists and persisted chat enablement/access state before any mirror or durable side effect | `test_admission_policy_rejects_chat_and_actor_before_any_side_effect` and `test_persisted_disabled_chat_overrides_default_open_policy` prove the production policy boundary |
| Hierarchical execution limits | Requirements 4.2 | Production config has one global task concurrency value; no tenant/app/group/plugin/provider limiter exists | Durable/fair leases enforce configured global plus tenant/app/group/plugin/provider caps without starving independent lanes |
| Wall-time and production cost budgets | Requirements 4.3 and acceptance 5 | The Harness consumes token/tool/write/cost only when declared; wall time is never consumed and production declares neither wall-time nor cost limits | Monotonic deadline enforcement and production limits terminate/recover deterministically; direct timeout/cost tests pass |
| Truthful write completion | Requirements 4.3/4.8 and acceptance 15 | A root run may attempt a failed/unverified write and then return model prose because the production root does not require verified effects | Any run that attempts an external write cannot complete without verified evidence; read-only requests remain valid |
| Root and cross-graph write locking | Requirements 4.4 and acceptance 7/13 | Durable target locks are acquired only for delegated writer children; root Agents in independent graphs may write the same target concurrently | Root write calls resolve targets, acquire/renew/release the same tenant-scoped durable locks, and reject overlap across graphs |
| Cross-group tool authority | Requirements 4.6 and security requirements | The group-Harness digest tool accepts a model-selected chat ID/name and relies only on upstream visibility | Group-originated digest work is bound to the trusted current chat; cross-group work needs a separately trusted authorization path |
| Runtime plugin isolation and readiness | Requirements 4.6 and acceptance 14 | Any plugin readiness failure aborts all Gateway startup; production tool authorization is not connected to current plugin health | Unhealthy optional capabilities reject dependent calls while unrelated IM/Harness work continues; mandatory ingress failure still blocks readiness |
| Checkpoint invalidation contract | Requirements 4.5 and Harness contract | Checkpoints validate Agent/resource/source/compactor versions, but not policy or tool-schema versions; retention GC deletes messages without deleting checkpoints derived from them | Checkpoints bind policy and tool-schema fingerprints; policy/schema mismatch and retention expiry discard affected checkpoints |
| Bounded shutdown and source supervision | Requirements 4.1 and operability | Gateway stop waits on an in-flight worker without a drain deadline/cancellation path; no Codex2Lark-level source health/reconnect supervisor is observable | Intake stops first, bounded drain checkpoints/releases work, timeout cancels safely, and source disconnect/reconnect state is health-visible |
| Capability plugin boundaries | Product definition and maintainability | Sheets, Base, and Whiteboard ship inside one `feishu-artifacts` plugin and Drive has no V3 capability plugin | Drive, Sheets, Base, and Whiteboard have separate manifests/lifecycles while sharing only public service ports |
| Readiness and trace coverage | Non-functional operability/observability | Offline doctor does not prove provider/event-source identity health, and not every task/node/tool/approval transition exposes a common trace binding | Startup/live health and content-safe trace correlation have direct production-path tests |

## Already proven

The audit retains the existing direct evidence for SQLite/WAL admission,
encryption, idempotent outbox, targeted purge, backup/restore/key rotation,
complete-turn compaction, durable controls and approvals, three-worker restart
recovery, 64-group fairness, exact target locking for delegated writers,
professional document create/edit workflows, group digest rendering, canary
selection, metrics, process control, and the opt-in live multi-group observer.

## Completion rule

Each row above is implemented documentation-first and committed as a key
feature. After all rows have direct tests, run the canonical validation, build
the wheel/sdist, validate the Codex plugin bundle, run offline Gateway readiness,
and record the unavoidable credential-dependent live checks separately. Only
then may [roadmap.md](roadmap.md) return all areas to `delivered` and the active
V3 goal be marked complete.
