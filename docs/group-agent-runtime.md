# Feishu IM capability plugin

## 1. Scope and boundary

`feishu-im` is the first capability plugin for the general Runtime defined in
[architecture.md](architecture.md). It serves every Feishu group in which the configured bot is
present and the application is available. An explicit mention of that bot starts
an Agent graph. The runtime acknowledges the request immediately, performs the
work asynchronously, and posts exactly one truthful terminal reply to the source
message or thread.

The default deployment is one machine and one Gateway service. It does not
require RabbitMQ, Redis, PostgreSQL, object storage, a public callback endpoint,
or a running Codex desktop task. Durable scheduling, Agent collaboration,
storage, identity, and policy are kernel services; this plugin owns only IM
normalization, context providers, message/file repositories, and publishers.

## 2. Runtime composition

```text
Feishu outbound long connection
  -> event consumers
  -> admission and normalization
  -> SQLite durable task store
  -> partitioned scheduler
  -> context and attachment loaders
  -> root and worker Agent Harnesses
  -> semantic Feishu services
  -> live verification
  -> root source-message reply
```

SQLite owns message metadata, normalized content, attachment metadata, delivery
state, run state, idempotency records, synchronization cursors, and context
checkpoints. Attachment bytes are stored in a managed encrypted file directory
and referenced by content hash from SQLite.

## 3. Event admission

The Gateway consumes at least these independent event keys:

- `im.chat.member.bot.added_v1` for deterministic owner-membership handling;
- `im.message.receive_v1` for group requests.

One event consumer failure must not stop the other consumer. Each source waits
for the lark-cli ready marker, drains diagnostics, restarts with bounded backoff,
and never exposes raw event payloads to the model.

A message starts an Agent run only when all conditions hold:

1. `chat_type` is `group`;
2. the sender is a user rather than a bot;
3. the mention list contains the current bot open ID;
4. the message ID has not already been admitted;
5. the chat and sender are still visible to the configured Feishu identity;
6. the normalized request contains non-empty user content after removing the bot
   mention placeholder.

Bot-authored messages, ordinary unaddressed chat, edited-event echoes, malformed
events, and duplicate deliveries do not invoke the model.

### Runtime API 1 IM boundary

The production transport uses the official `lark-channel-sdk` package pinned to
`1.0.0`. It runs in WebSocket mode with strict security limits, resolves the
connected bot identity before readiness, and passes normalized values through a
Codex2Lark-owned adapter. SDK objects, raw OpenAPI clients, and SDK-managed cache
paths never cross the plugin boundary or become model tools.

The plugin's canonical inbound value contains tenant/app/event identity, chat
and message identity, optional thread/root/parent identity, sender identity and
type, message type, safe normalized body text, explicit mention identities,
attachment references, source create/update times, and receive time. Exact
admission compares the configured bot open ID with the explicit mention list;
an SDK convenience boolean is not sufficient evidence.

Admission is split into two durable operations with safe replay semantics:

1. upsert the encrypted normalized chat/message/attachment mirror using source
   update time and tombstone precedence;
2. atomically insert the normalized runtime event, one pending task, and one
   acknowledgement outbox intent.

A crash between the two operations may leave an unused mirror row but cannot
acknowledge forgotten work. Redelivery completes admission. The runtime event's
`(tenant, app, event_id)` uniqueness prevents a second task or acknowledgement.
Ignored events return a typed reason and create neither a task nor an outbox
intent.

The Channel adapter registers only message and bot-membership callbacks. A
callback converts the SDK value into the canonical inbound value and submits it
to a bounded Runtime-owned queue; it never calls the model or performs document
work. Queue saturation rejects the callback with a content-free diagnostic so
the source can redeliver instead of silently dropping accepted work.

The IM outbox publisher accepts only typed acknowledgement, progress, approval,
and terminal payloads. It replies to the source `message_id`, preserves thread
placement, passes the durable outbox idempotency key as the SDK request UUID,
and treats an SDK result as sent only when success and an upstream message
reference are both present. Failed or ambiguous sends remain retryable and are
never converted into task completion.

## 4. Acknowledgement and terminal replies

After admission and durable task creation, the bot replies to the source message
without waiting for model inference. The default acknowledgement is:

> Got it - I will take care of this and come back as soon as it is finished.

Localized response templates are configuration resources, not model-generated
prompts. The default Chinese profile uses a gentle, natural, and concise tone;
it must not imitate a demographic stereotype or reduce technical accuracy.

Every acknowledged task eventually produces exactly one of these terminal
states:

- `completed`: the requested observable result passed verification;
- `blocked`: required user input, authorization, or an external decision is
  missing;
- `failed`: execution ended without the requested result;
- `cancelled`: an authorized cancellation ended the run.

The terminal reply states the status explicitly, summarizes completed work,
links created or modified Feishu resources, reports verification warnings, and
invites the user to ask a follow-up question. A model message alone is never
evidence of completion.

Acknowledgement and terminal replies use deterministic idempotency keys derived
from the source `message_id` and reply kind.

For one admitted request, the acknowledgement intent is always leased before
progress, approval, or terminal intents, including when their timestamps are
identical after a fast model response. Delivery retry preserves this causal
order so the user never sees “completed” before “received.”

## 5. Message collection

The receive event is a wake-up signal and routing reference. Before inference,
the runtime fetches authoritative live message data from Feishu.

Context selection is relationship-first:

1. always fetch the triggering message by `message_id`;
2. for a thread, fetch the root and bounded recent thread replies;
3. for a direct reply, fetch the referenced parent and available root chain;
4. for an ordinary group mention, fetch bounded messages preceding the trigger;
5. expand a wider time range only when the request explicitly asks for it;
6. preserve chronological order, sender attribution, timestamps, mentions,
   edits, recalls, and attachment references.

Proposed default limits are configuration values:

- ordinary group context: 30 preceding messages or two hours;
- thread context: root plus 50 recent replies;
- one model-visible item: 10,000 tokens maximum;
- complete collected context: bounded by the selected model policy;
- pagination: incomplete pagination is surfaced and never treated as complete.

The active mention message is the user request. Earlier group messages, quoted
messages, filenames, attachment content, and Feishu documents are untrusted
evidence, not developer or system instructions.

### Runtime API 1 relationship context

The IM context provider receives a trusted tenant/app/chat/message binding, not
free-form model arguments. It first refetches the trigger message. It then asks
the source adapter for either the bounded thread/reply relationship or messages
from the same chat within the configured lookback window. Every returned item
must match the bound tenant, app, and chat before it is mirrored or emitted.

The provider deduplicates by message ID, orders by source creation time, excludes
the trigger from background evidence, and emits one `ContextEvidence` per live
message with `im.message:<message_id>` provenance and its source update version.
Recalled/deleted items become mirror tombstones and never become model evidence.
An incomplete page is a typed warning; the provider never labels incomplete
history as complete. Local mirror rows are used for restart and reconciliation,
but the provider does not silently fall back to stale local content when a
required live read fails.

## 6. Attachment collection and parsing

The event path stores attachment references before downloading bytes. An
attachment reference includes source chat/message, resource key, filename,
media type, declared size, sender, and creation time.

Bytes are downloaded only when one of these conditions holds:

- the triggering message directly attaches the resource;
- the user explicitly identifies the resource;
- context planning determines that the resource is necessary for the authorized
  task and policy permits the type and size.

Supported first-release parsing routes are:

| Type | Handling |
|---|---|
| text, Markdown, JSON, CSV | bounded text decoding and structural validation |
| image | image input with bounded dimensions and bytes |
| PDF | bounded text extraction and optional native model file input |
| DOCX | paragraphs, headings, tables, and links |
| XLSX | bounded sheets, typed cells, formulas, and dimensions |
| PPTX | slide titles, body text, notes, and media references |
| audio and video | metadata only in the first release |
| archive, executable, and script | store when policy permits; never unpack or execute automatically |

Every parser returns text or structured evidence with source provenance,
truncation warnings, parser version, and content hash. Parsing never executes
macros, embedded programs, formulas, links, or instructions found in the file.

### Runtime API 1 attachment ingest and parsers

The first implementation defaults to 20 MiB per downloaded attachment, 50 MiB
total declared uncompressed Office ZIP members, 1,000 ZIP entries, a 100:1
member compression-ratio ceiling, and 200,000 output characters. These are hard
upper bounds configurable only downward by a task policy. A declared size above
the limit is rejected before download; actual bytes are checked again.

Download authorization is a trusted `AttachmentLoadRequest` bound to tenant,
app, chat, message, and resource key. The repository must already contain that
reference. Downloaded plaintext exists only in memory for the bounded ingest;
the managed blob store writes an authenticated encrypted envelope through an
owner-only temporary file and atomic link. SQLite then records the blob
reference. A crash before that transaction may leave only an encrypted orphan,
which maintenance removes; a database row never points at plaintext.

Text/Markdown/JSON/CSV use bounded decoding and validation. PDF uses the pinned
`pypdf` parser without following links. DOCX/XLSX/PPTX use bounded ZIP member
selection and `defusedxml`; formulas are rendered as inert text and never
evaluated. Images produce a typed managed-image reference plus metadata. Audio
and video produce metadata only. Archives, scripts, and executables return a
blocked parser result and are never unpacked or executed. Parser output is
encrypted and keyed by content hash, parser ID/version, and policy version.

## 7. Durable storage

The store uses one SQLite database in WAL mode and one managed file directory.
The data directory is configured explicitly or resolved from the operating
system's application-data convention. It must not live inside the source
repository.

Logical tables are:

```text
chats
messages
attachments
file_blobs
sync_cursors
tasks
runs
run_events
context_checkpoints
idempotency_keys
```

Message rows are keyed by tenant/app/chat/message identity. Upserts compare live
`update_time`; recalls and deletions become tombstones rather than silently
leaving stale content. Losing access to a chat disables retrieval and schedules
the configured purge behavior.

File bytes are named by a cryptographic content hash, deduplicated, encrypted at
rest, and never served by a general-purpose local HTTP endpoint. The encryption
master key is supplied by an operating-system keychain, environment secret, or
external secret provider and is never stored in SQLite.

SQLite transactions implement the durable queue and outbox. A task is
acknowledged only after its state transition and associated reply intent commit
together. Leases allow unfinished work to return to the queue after process
restart. Observable Feishu writes remain idempotent and are verified before a
task becomes `completed`.

## 8. Retention and deletion

Retention is configurable globally and per chat. Conservative defaults are:

- normalized group messages: 90 days;
- attachment bytes and parser output: 30 days;
- context checkpoints: 90 days;
- completed/failed run metadata: 30 days;
- delivery and idempotency metadata: 7 days.

The operator can disable persistence for a chat, purge one chat, purge one
message and its unreferenced blobs, or purge all business data. Garbage
collection is reference-aware and bounded. A disk high-water mark stops new file
downloads before it threatens Gateway availability.

Deletion, recall, retention expiry, and access loss must remove derived parser
content and context checkpoints as well as the source row and unreferenced blob.

## 9. Context management

The model context is assembled progressively in this order:

1. immutable system and security policy;
2. versioned AgentDefinition and selected Skills;
3. trusted chat, identity, and authorization bindings;
4. the active mention request;
5. bounded thread/reply/recent-chat evidence;
6. attachment evidence loaded on demand;
7. typed tool observations;
8. a working checkpoint when compaction is required.

Context is append-oriented during a run. The builder avoids rewriting a stable
prefix, enforces hard per-item and total token budgets, keeps complete
assistant/tool-call pairs, and reserves capacity for tool results and the final
answer.

When the budget is exceeded, it removes irrelevant evidence first, then
summarizes older complete turns while retaining the active request, completed
actions, resource identifiers, verification results, blockers, and next action.
Persistent checkpoints are derived data and carry source message IDs so they can
be invalidated after edits, recalls, deletions, or retention expiry.

## 10. Session and concurrency model

The durable session key is derived from trusted values:

```text
tenant_id + app_id + chat_id + thread_id/root_message_id
```

One session key has at most one active Agent run. Separate threads may run
concurrently subject to global, app, chat, and model-provider limits. Event
ingestion remains ordered per chat even when independent Agent sessions execute
in parallel.

Follow-up messages in the same Feishu thread reuse the durable session identity,
but the runtime still reconciles live Feishu messages before using local content.
No context crosses a chat or tenant boundary.

The durable task scheduler leases at most one task per SessionKey and excludes
any SessionKey that already has a live lease. A batch may execute independent
SessionKeys concurrently. A handler returns a typed terminal task result and
its terminal reply intent commits in the same transaction. Transient provider
failures return the task to pending with bounded delay; retry exhaustion commits
a failed terminal reply rather than leaving an acknowledged request silent.

## 11. Single-node failure model

The single machine is one failure domain. SQLite and the encrypted attachment
directory survive process restart, but a disk or host loss can lose the mirror
unless the operator backs up both the database, encrypted blobs, and the external
encryption key.

The Gateway recovers expired task leases, resumes queued work, and reconciles
pending reply intents at startup. It never claims exactly-once external side
effects; it provides at-least-once execution with idempotent writes and live
verification.

## 12. Operational boundaries

- The model never receives raw database access, arbitrary filesystem access,
  shell execution, or raw lark-cli arguments.
- SQL is encapsulated behind repository ports and parameterized statements.
- Local paths never appear in model-visible content or user-facing replies.
- Logs contain identifiers, timings, state transitions, sizes, and hashes, but
  not message bodies, parsed file content, credentials, or model prompts.
- Backups are an operator responsibility and must protect the database, blobs,
  and key material consistently.
