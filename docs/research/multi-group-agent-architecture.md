# Multi-group agent architecture research

## 1. Research question

Codex2Lark must support one logical GPT/agent serving many Feishu groups while
remaining available when an interactive Codex task and its stdio MCP process do
not exist. The architecture must preserve per-group ordering, allow cross-group
parallelism, avoid a local business-content database, and keep the default
deployment proportional to current reliability needs.

This document records the evidence used for the V2 architecture decision. It is
not a claim that Codex2Lark will embed or fork every project considered.

## 2. Reference projects

| Project | Relevant pattern | What Codex2Lark adopts | What Codex2Lark does not adopt |
|---|---|---|---|
| [OpenAI Harness Engineering](https://openai.com/index/harness-engineering/) | Humans specify intent and feedback loops; repository knowledge, architecture constraints, tests, observability, and maintenance make the environment legible to Agents | Treat the Harness and its feedback environment as the product core; version prompts, tools, policies, docs, and evals together | Prompt-only agent design or an unstructured collection of instructions |
| [OpenAI Codex](https://github.com/openai/codex) | One Codex Core owns the Agent loop, thread lifecycle, tools, sandbox/approval policy, skills, context management, and compaction; App Server exposes the same Harness to many clients | One reusable Harness Core, explicit thread/turn/item protocol, stable context layers, approval gates, compaction, steering, and multiple client surfaces | Coding-specific shell/file tools and local repository persistence as the Feishu business-data model |
| [Pi](https://github.com/earendil-works/pi) | Small provider-neutral Agent core, explicit `AgentMessage -> transformContext -> LLM Message`, typed tools, before/after hooks, event streaming, pluggable sessions, skills/resources, steering/follow-up, and compaction | Minimal composable Agent loop, provider port, message transformation boundary, hookable policy/verification, pluggable session manager, and observable events | Pi's local JSONL session files and coding-agent filesystem assumptions |
| [LangBot](https://github.com/langbot-app/LangBot) | Mature multi-platform IM adapters, event conversion, pipelines, external Agent runners, one pipeline bound to multiple bots | Channel adapter boundary, normalized events, pipeline/runner separation, stable group session key | Its full bot platform, UI, plugin market, and local conversation database |
| [AstrBot](https://github.com/AstrBotDevs/AstrBot) | Always-on multi-platform Agent chatbot with Feishu support, plugins, MCP, sandboxing, and 24-hour deployment model | Treat IM ingress as a deployed service rather than an interactive developer process | Its all-in-one runtime and broad plugin surface |
| [Temporal](https://github.com/temporalio/temporal) | Durable workflows, task queues, worker polling, replay, retries, and idempotent activities | Separate ingress from workers; make external side effects idempotent; design workers to resume after failure | Temporal in the first production slice, because workflow history would add a durable state system whose retention and payload policy are unnecessary for simple message turns |
| [RabbitMQ](https://www.rabbitmq.com/docs/reliability) | Publisher confirms, manual consumer acknowledgements, redelivery, replicated queues, bounded prefetch, dead-letter handling | A future durable `TaskQueue` adapter when restart-safe accepted work or multiple workers become required | Making a broker mandatory for the default single-service profile or claiming exactly-once delivery |
| [LangGraph Agent Server](https://langchain-ai.github.io/langgraph/concepts/langgraph_server/) | API tier separated from task-queue workers, thread serialization, independently scalable pools | Separate ingress/API from Agent workers; serialize one conversation while running many conversations concurrently | Its mandatory PostgreSQL checkpoint model for the initial no-content-persistence deployment |
| [OpenAI Agents SDK](https://github.com/openai/openai-agents-python) and [Responses API](https://developers.openai.com/api/reference/cli/resources/responses/methods/create) | Backend Agent runner, tools, guardrails, tracing, background responses, conversations | A callable backend Agent Runtime with strict tools and per-run metadata; one agent definition can serve many independent runs | Treating the Codex desktop task as a daemon or using server-managed conversation history by default |

LangBot's adapter documentation makes the channel boundary explicit: an
adapter receives platform events, converts them to a platform-neutral event,
and converts the result back to the platform protocol. Its pipeline model also
passes `launcher_id`, `sender_id`, `session_id`, and `conversation_id`, and the
same pipeline can serve multiple bots. Those are the closest open-source
precedents for the N-group routing problem.

Temporal demonstrates why a deployed service, rather than an interactive client,
must own execution. RabbitMQ's reliability guidance establishes what must be
added when accepted work needs to survive process failure: durable queue state,
acknowledgements, redelivery, and duplicate-safe handlers. Those properties are
valuable but are not free operationally, so they define the durable profile
rather than the default profile.

OpenAI's Responses API can run model work and call typed custom tools from a
backend service. Its data controls matter to this project: Responses are stored
by default unless the request and organization policy say otherwise, and
background mode retains response data temporarily for polling. V2 therefore
defaults to foreground `store=false` runs and treats background mode as an
explicit deployment policy, not a transparent optimization.

OpenAI's Codex design separates the Harness from every client surface. Codex
Core owns the loop, thread lifecycle, authentication, tool execution, skills,
sandbox policy, and persistence; App Server exposes that same core through a
bidirectional protocol to the CLI, IDE, desktop, and other clients. The Codex
Agent loop repeatedly samples the model, executes requested tools, appends tool
results, and stops on a final assistant message, while automatic compaction
keeps long runs inside the context window.

Pi reaches a similar result with a smaller and more provider-neutral design. Its
Agent core distinguishes application messages from LLM messages, runs an
explicit transformation boundary before inference, exposes typed tool hooks and
ordered lifecycle events, supports steering and follow-up queues, and lets
callers choose an in-memory or durable SessionManager. Pi's ResourceLoader and
extension hooks demonstrate how Skills, context files, prompts, and policy can
evolve without coupling the Agent loop to one UI or channel.

## 3. Findings

### 3.1 MCP is not an inbound event runtime

Stdio MCP is an excellent boundary for an interactive Codex or ChatGPT host to
invoke semantic Feishu operations. Its process lifetime belongs to that host.
It cannot guarantee that a process exists when Feishu sends an event and cannot
wake a closed Codex task. Network reconnection inside MCP only helps while the
MCP process itself remains alive.

### 3.2 One agent means one policy, not one process

One logical agent is a versioned set of instructions, tools, guardrails, model
policy, and authorization rules. Many worker processes may execute that same
agent definition. A single process would create a failure domain and throughput
bottleneck; a single shared conversation would leak context between groups.

The stable isolation key is:

```text
tenant_key / app_id / chat_id / optional_thread_root_id
```

Events sharing the key are serialized. Different keys may run concurrently.
The sender ID is an authorization and attribution input, not the primary group
conversation key.

### 3.3 Restart-safe delivery requires bounded operational persistence

It is impossible to promise recovery after Gateway or Worker failure while
keeping every event only in volatile process memory. V2 Lite explicitly does
not make that promise. It uses bounded process memory and accepts loss of
in-flight work on process exit in exchange for a one-service deployment with no
broker, database, or cache.

If a deployment requires restart-safe delivery, the smallest acceptable
exception to statelessness is an external managed broker containing a
short-lived event-reference envelope and acknowledgement state.

The envelope contains identifiers such as event ID, event type, tenant/app,
chat ID, message ID, creation time, attempt count, and trace ID. It does not
contain message bodies, document bodies, attachments, generated prompts, or
model output. Workers fetch live content from Feishu when processing begins.
Queue TTL, dead-letter TTL, and deletion after acknowledgement bound retention.

This is operational reliability metadata, not a second business-data source of
truth. No such metadata is written to the developer workstation or repository.

### 3.4 Deterministic events should bypass the model

Bot-added membership, permission checks, deduplication, notification delivery,
and fixed routing rules do not need model judgment. They run as deterministic
activities. Model calls are reserved for summarization, document composition,
question answering, classification that cannot be expressed as policy, and
other semantic work.

### 3.5 Long connection is the default transport

Feishu long connection is initiated outbound, requires internet access but no
public IP or domain, and can receive events for all groups served by one
application. It therefore minimizes the deployment surface without coupling
availability to Codex, stdin, or MCP—as long as it runs in a standalone Gateway
service. HTTPS Webhook remains an optional ingress adapter for public callback,
serverless, or horizontally scaled ingress topologies.

### 3.6 The Harness, not the model, is the Agent product

The model supplies reasoning; the Harness supplies identity, instructions,
context, tools, policy, execution, observations, compaction, approvals,
verification, recovery, and termination. Changing the Harness can materially
change capability and reliability without changing the underlying model.

Codex2Lark therefore needs a first-class Harness Core between routing and model
providers. The Harness consumes normalized `AgentMessage` objects rather than
raw Feishu payloads, transforms only approved context into model input, runs the
tool loop, emits structured lifecycle events, and finishes only after outcome
verification or an explicit blocked/failed state.

MCP remains a semantic tool protocol. It is not the Harness control protocol:
thread creation, run streaming, steering, cancellation, approval, and recovery
need their own typed run interface, just as Codex App Server exposes more than
MCP tool calls.

## 4. Decision

Codex2Lark V2 is a dual-plane system:

1. **Interactive authoring plane:** Codex/ChatGPT -> MCP -> shared Feishu
   capabilities.
2. **Realtime automation plane:** Feishu long connection -> standalone Event
   Gateway -> bounded `TaskQueue` -> per-chat Router -> deterministic or Agent
   handlers -> shared Feishu capabilities.

Both planes converge on one **Agent Harness Core**. The Harness has a
provider-neutral model port, an explicit Agent loop, progressive ResourceLoader,
group-scoped ContextBuilder, typed ToolRegistry, policy hooks, verifier hooks,
compaction, steering/follow-up queues, and a pluggable SessionManager. The
default session implementation reconstructs context from live Feishu and keeps
only in-flight state in memory; no local JSONL or SQLite history is used.

The MCP process does not own event subscriptions. The same installation runs
the independent `codex2lark gateway` command whenever real-time automation is
required.

The default `InMemoryTaskQueue` is bounded and feeds a fixed partition
dispatcher. A stable hash of SessionKey selects a partition; one coroutine per
partition preserves ordering while partitions run concurrently. RabbitMQ or a
managed queue may later implement the same port when measured reliability or
scale requires it. Temporal remains optional for multi-day approval or
human-in-the-loop workflows.

The Agent Runtime initially uses the OpenAI Responses API or Agents SDK as a
backend service. It receives a fresh, bounded live context assembled from
Feishu for each run, uses `store=false` by default, and exposes only semantic
Codex2Lark tools. Interactive Codex remains a supported client of MCP but is not
part of the production availability path.

## 5. Rejected alternatives

| Alternative | Rejection reason |
|---|---|
| Keep the event listener in stdio MCP | Cannot receive events when the host task is closed and cannot provide an availability SLO |
| Run one permanent Codex desktop task | Not a supported service boundary, creates one failure domain, and cannot safely isolate N group contexts |
| Require RabbitMQ in V2 Lite | Provides restart-safe delivery but adds a broker lifecycle, monitoring, backups/upgrades, and multi-node complexity before the current workload requires it |
| Pretend an in-memory queue is durable | It loses accepted events on restart and cannot coordinate multiple replicas; V2 Lite documents this limitation explicitly |
| Persist complete group messages in PostgreSQL | Violates the requirement that Feishu remain the business-content source of truth |
| Use one global model conversation for every group | Causes cross-group context and authorization leakage |
| Invoke the model for every Feishu event | Adds latency and cost to deterministic operations and expands the prompt-injection surface |
| Adopt Temporal immediately for every message | Adds history persistence and operational complexity before workflows require durable multi-step orchestration |

## 6. Validation questions for implementation

- Can two groups send messages concurrently without sharing context?
- Are messages from one group processed in creation order?
- Does queue capacity apply backpressure rather than growing without bound?
- Does Gateway restart recover the long connection while clearly reporting that
  prior in-memory work is not replayed?
- Can a durable queue adapter be introduced without changing the source or
  deterministic handlers?
- Does stopping Codex or MCP leave Feishu event handling available?
- Can the Agent Runtime be disabled while deterministic bot-added automation
  remains healthy?
- Are model storage, tracing, and background-mode retention visible deployment
  choices?
