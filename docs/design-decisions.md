# V3 design decisions

This record explains choices that constrain implementation. V3 does not preserve
internal compatibility with V2; these decisions may replace existing code.

| Topic | Decision | Rejected alternative | Revisit trigger |
|---|---|---|---|
| Product boundary | General Feishu Agent Runtime with capability plugins | IM-specific bot with domain logic in handlers | None; this is the product direction |
| Architecture method | Harness contracts, evals, and observability own behavior | Prompt-centric orchestration | Only if a contract is proven unnecessary by evals |
| Agent topology | Rooted task tree plus acyclic dependencies | Shared peer swarm or one global Agent | A validated workflow requires decentralized ownership |
| Delegation | Explicit task brief, output schema, scoped context/tools/budget | Full parent transcript and implicit authority inheritance | Evals show selected context cannot support a required workflow |
| User outcome | Root Agent alone publishes terminal result | Every worker replies independently | A plugin defines a separate user-facing subworkflow |
| Plugin model | Trusted typed Python capability plugins plus declarative resource packages | Pi-style unrestricted hooks, model-installed code, arbitrary directory execution | Signed sandboxed extension runtime is designed and threat-modeled |
| Cross-plugin work | Harness orchestration through semantic capability IDs | Plugins importing one another's internals | Measured latency requires a typed composite capability |
| Deployment | One long-running process on one machine | Microservices, Kubernetes, or one process per group | Multiple-host availability or independent scaling becomes required |
| Event ingress | Feishu outbound long connection | Public Webhook as default | Network topology prohibits outbound long connections |
| Durable queue | SQLite leases and transactional outbox | In-memory queue or mandatory RabbitMQ | Multiple worker hosts or measured SQLite queue limits |
| Business data | Authorized encrypted local mirror with TTL and reconciliation | Never persist content or treat local storage as authoritative | Regulatory policy prohibits selected local data classes |
| File storage | Encrypted managed blob directory plus SQLite metadata | Database BLOBs, public file server, or object store | Blob scale or multi-host deployment exceeds local disk |
| Data schema | Kernel tables plus typed plugin-owned tables | Universal JSON/EAV business-object table | A genuinely schemaless upstream object is documented |
| Context | Progressive source-attributed evidence and structured checkpoints | Entire group transcript or opaque memory | Evals demonstrate a bounded missing-context class |
| Compaction | Complete-turn compaction preserving tool-call/result pairs and verified state | Free-form conversation summary | Model/provider supplies a stronger auditable primitive |
| Identity | Trusted bot/delegated-user binding outside model context | Model selects credentials or actor | Never |
| Tools | Versioned semantic capabilities with policy and verification | Raw lark-cli, shell, SQL, or generic OpenAPI | Never for model-visible tools |
| Completion | Observable read-back verification | Trust model/tool success text | Upstream has no observable verification; then report `uncertain` |
| Configuration | Versioned AgentDefinition and operator configuration | Mutable behavior inferred from chats | A governed Feishu control UI is implemented |
| Model provider | Replaceable provider port; initial production provider selected during implementation | Make Codex desktop/stdio MCP the daemon | Codex exposes a supported always-on embeddable service contract |
| Backward compatibility | No V2 internal API or behavior compatibility requirement | Layer adapters and shims over old structure | Only operator data migration approved by product scope |

## Sources and adaptations

The design was informed by these primary sources:

- [OpenAI Harness Engineering](https://openai.com/index/harness-engineering/):
  repository legibility, executable constraints, feedback loops, and evaluation
  as part of the engineering environment;
- [OpenAI Codex App Server](https://github.com/openai/codex/blob/main/codex-rs/app-server/README.md):
  Core/App Server separation, threads, turns, streaming lifecycle, approvals,
  interruption, and resumable clients;
- [OpenAI Codex multi-Agent implementation](https://github.com/openai/codex/tree/main/codex-rs/core/src/tools/handlers):
  explicit spawn/message/follow-up/wait/interrupt operations, rooted task paths,
  bounded delegation, and independent Agent state;
- [Pi SDK](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/sdk.md):
  a small embeddable Agent session, replaceable session/runtime services,
  lifecycle events, ResourceLoader, steering, and follow-up;
- [Pi compaction](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/compaction.md):
  complete-turn cut points, preservation of tool-call/result relationships,
  structured summaries, and branch-aware accumulated state;
- [Pi extensions](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/extensions.md):
  composable tools and lifecycle events.

Codex2Lark adopts the architectural lessons, not the local coding-agent threat
model. Feishu capabilities remain typed, permission-bound, auditable, and
verified, and executable plugins remain an operator-controlled trust boundary.
