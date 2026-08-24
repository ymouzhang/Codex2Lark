# Delivery roadmap

## Completed foundation

The repository already provides a local stdio MCP plugin, strict Feishu
authoring tools, safe lark-cli execution, Docs create/inspect/edit, managed Drive
folder resolution, verified notifications, Whiteboard/Sheets/Base operations,
and bounded group-chat digest publishing.

The bot-added event contract and membership behavior are implemented. The V2
Lite refactor extracts their lifecycle from MCP into an independently operated
long-connection Gateway with bounded in-memory dispatch.

## V2 Phase 0: Harness specification and eval baseline

- Add versioned `AgentMessage`, `AgentDefinition`, `RunEvent`, `SessionKey`,
  `Outcome`, policy, approval, and model-provider schemas.
- Add the provider-neutral Agent loop with an in-memory SessionManager.
- Add progressive ResourceLoader behavior for Skills, prompts, policies, and
  context references.
- Wrap the existing semantic Feishu services as typed Harness tools.
- Add `before_tool_call`, `after_tool_call`, outcome verification, cancellation,
  steering, follow-up, and hard run budgets.
- Establish deterministic evals for routing isolation, prompt injection, tool
  use, approval, compaction, and truthful completion before model integration.

Exit: a recorded normalized request can run through the Harness with a fake
model and fake Feishu environment, producing a verified terminal outcome and a
complete ordered event stream.

## V2 Phase 1: Standalone lightweight event plane

- Extract the lark-cli event consumer from MCP lifespan into a standalone
  long-connection Gateway.
- Define the minimal `EventReference` schema.
- Add a bounded in-memory `TaskQueue` adapter and fixed partition dispatcher for
  per-chat ordering and cross-chat concurrency.
- Implement deterministic bot-added membership handling without a model.
- Keep the existing digest-time membership check as recovery defense.
- Remove the production startup dependency between MCP and the event consumer.

Exit: with Codex and MCP stopped, a running Gateway receives bot-added events
and invites the configured owner idempotently. No public callback endpoint,
database, or message broker is required; queued work does not survive Gateway
exit.

## V2 Phase 2: N-group control and routing

- Create the `Codex2Lark Control` Feishu Base schema and resolver.
- Implement group enrollment, disablement, onboarding card, owner membership,
  AgentDefinition selection, trigger policy, tool profile, and approval policy.
- Implement the trusted SessionKey and per-key single-active-run scheduler.
- Allow cross-group parallelism with per-app, tenant, group, and provider rate
  limits.
- Add anti-loop, bot-message filtering, exact mention/command admission, and
  group removal handling.
- Add tests with concurrent events from multiple chats and tenants.

Exit: N groups share one AgentDefinition while preserving ordering and complete
context, authorization, target, and failure isolation.

## V2 Phase 3: Production Agent Runtime

- Add the OpenAI Responses provider behind the Harness model port.
- Default to `store=false` and fresh bounded context from live Feishu.
- Add message/thread context assembly, sender attribution, image policy, and
  attachment metadata handling.
- Add Feishu progress cards and final verified replies.
- Add model/tool/cost/time budgets, compaction, retry classification, and
  provider circuit breakers.
- Keep lifecycle and permission workflows deterministic when the model provider
  is unavailable.
- Add evaluation gates comparing AgentDefinition and Harness versions.

Exit: an addressed message in any enrolled group starts an isolated Harness run,
uses authorized semantic tools, and replies with a verified outcome without a
running Codex task.

## V2 Phase 4: Service-native Feishu adapter and identity

- Add `FeishuOpenApiAdapter` for long-running services; retain `LarkCliAdapter`
  for local MCP and development.
- Add an Identity Broker for bot and delegated-user credentials held in an
  external secret provider.
- Define OAuth refresh, credential rotation, tenant isolation, and revocation.
- Move high-frequency worker paths away from per-operation CLI subprocesses.
- Preserve identical semantic tool and verification contracts across adapters.

Exit: Gateway and workers scale horizontally without sharing a workstation,
lark-cli credential store, or process-local token state.

## V2 Phase 5: Harness Run API and rich interaction

- Add a bidirectional typed Run API for start, stream, steer, follow-up,
  approval, cancel, resume, and inspect.
- Keep MCP as the semantic tool surface for interactive Agent clients.
- Add interactive Feishu approval cards and authorized decision routing.
- Add optional encrypted short-TTL checkpoints for explicitly approved
  long-running workflows.
- Add run forking and replay only in redacted evaluation environments, not as a
  business-content store.

Exit: Feishu, web administration, tests, and future clients observe and control
the same Harness without reimplementing the Agent loop.

## V2 Phase 6: Production hardening

- Define reliability thresholds that justify a durable queue: restart-safe
  accepted work, sustained backlog, multiple worker replicas, or a strict SLO.
- Add RabbitMQ or a managed queue as an optional `TaskQueue` adapter only when a
  deployment crosses those thresholds.
- Optionally add an authenticated HTTPS Webhook source for deployments that
  prefer a public callback endpoint over the default outbound long connection.
- Deploy redundant Gateway and Agent workers across failure domains where the
  chosen reliability profile requires them.
- Add queue depth/age, oldest event, provider latency, tool failure,
  verification, dead-letter, token/cost, and policy-version metrics.
- Add canary AgentDefinition and Harness rollout with automatic eval regression
  gates and rollback.
- Add chaos tests for Gateway loss, Worker death, broker failover, Feishu rate
  limits, provider outage, duplicate delivery, and partial external writes.
- Add operational garbage collection for expired metadata and stale policies.

Exit: production SLOs exclude Codex/MCP availability, content-free observability
detects failure, and a bad Harness or policy version can be rolled back safely.

## Deferred

- Multi-day durable approval/workflow orchestration with Temporal.
- Cross-session semantic memory outside live Feishu content.
- Cross-group workflows without explicit source/destination authorization.
- A general-purpose arbitrary OpenAPI or shell tool.
- A proprietary copy of Feishu business content.
