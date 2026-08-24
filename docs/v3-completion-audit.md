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
| Hierarchical execution limits — durable task layer resolved | Requirements 4.2 | Production task leasing now persists trusted scope and atomically enforces fair global/tenant/app/group caps plus SessionKey ordering; shared plugin/provider call-level gates remain open | Direct saturation, fairness, concurrent leasing, and expired-lease tests prove the durable layer; plugin/provider gates are still required |
| Wall-time and production cost budgets — resolved | Requirements 4.3 and acceptance 5 | The Harness persists cumulative active monotonic time and bounds model/tool awaits; the production adapter calculates micro-USD from operator-supplied current prices and every root definition declares wall/cost ceilings | Direct timeout/resume, price calculation, configuration, and production-definition tests prove deterministic enforcement |
| Truthful write completion — resolved | Requirements 4.3/4.8 and acceptance 15 | Every unverified write/destructive result is durably tracked and blocks a completed terminal state, independent of model prose or the definition's minimum-effect flag | Direct failed/uncertain/policy-denied write tests and read-only completion tests prove the outcome gate |
| Root and cross-graph write locking | Requirements 4.4 and acceptance 7/13 | Durable target locks are acquired only for delegated writer children; root Agents in independent graphs may write the same target concurrently | Root write calls resolve targets, acquire/renew/release the same tenant-scoped durable locks, and reject overlap across graphs |
| Cross-group tool authority — resolved | Requirements 4.6 and security requirements | The group-Harness digest schema no longer accepts a target; execution and lock resolution inject the trusted current `ToolContext.chat_id`, and delegated cross-chat declarations fail before service access | Direct root/delegated authority tests prove same-chat execution and zero-service-call rejection; interactive MCP remains separately explicit |
| Runtime plugin isolation and readiness — resolved | Requirements 4.6 and acceptance 14 | Production classifies IM ingress as mandatory and business capability plugins as isolated optional providers; tool policy rechecks trusted owning-plugin health immediately before execution | Direct startup isolation, mandatory failure, tool denial, and live recovery tests prove unrelated work remains available |
| Checkpoint invalidation contract — resolved | Requirements 4.5 and Harness contract | Encrypted checkpoints bind Agent/resource/source/compactor/policy versions and a canonical allowed-tool schema fingerprint; retention GC deletes checkpoints derived from expiring message/attachment sources in the same transaction | Direct policy/schema mismatch recovery and retention-expiry tests prove stale tool calls/context are discarded |
| Bounded shutdown and source supervision — shutdown resolved | Requirements 4.1 and operability | Gateway now stops intake first, bounds every lifecycle phase, and timeout cancellation immediately defers durable tasks without spending retries; Codex2Lark-level disconnect/reconnect health remains open | Direct cancellation/drain tests pass; observable bounded-backoff source supervision is still required |
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
