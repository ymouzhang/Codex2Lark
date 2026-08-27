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
- `im.chat.member.bot.deleted_v1` for immediate access revocation and cleanup;
- `im.message.recalled_v1` for immediate message tombstoning;
- `im.message.receive_v1` for group requests.

One event-handler failure must not stop the other event path. The official
Channel SDK owns one outbound WebSocket connection and resolves bot identity
before readiness. Its message and membership callbacks perform only bounded
normalization and durable admission; raw payloads are never exposed to the model.

Shutdown stops admission first and gives the active durable task batch a
configured finite drain window. On timeout, cancellation propagates through the
Harness/model/tool awaits; each leased task is atomically deferred, its retry
attempt is restored, and the last complete checkpoint remains the recovery
point. No new batch is leased during shutdown.

When `botAdded` arrives, the callback durably admits a normalized reference and
returns after that transaction commits. Admission creates one
`im.ensure_owner_membership` task keyed by tenant/app/event. The task uses the
operator-bound lark-cli identities to inspect members, add the current
authenticated user only when absent, and verify user access to the group.
Retries are bounded and duplicate event delivery cannot repeat the logical
membership action. This path starts immediately after the event; it never waits
for a user mention or a Codex/MCP process.
After membership verification succeeds, the same task restores a previously
revoked local chat binding; delayed receive events cannot restore it by
themselves.

Recall and bot-removal callbacks use the same pre-acknowledgement durability
boundary as receive and bot-added callbacks. A recall event admits an
`im.invalidate_message` task containing only trusted identifiers and source
time. The task writes a monotonic tombstone, deletes attachment/parser rows,
invalidates checkpoints that cite the message, and reclaims blobs only after
the database proves that no live attachment references them. An older receive
or backfill cannot overwrite the tombstone.

A bot-removal event admits an `im.revoke_chat_access` task. The task disables
the chat before any later retrieval, cancels pending work for that chat, removes
the chat's locally retained IM content and derived checkpoints, and reclaims
unreferenced blobs. The event produces no group reply because the bot no longer
has a valid delivery channel.

Feishu does not publish a separate event for ordinary message edits. Context
construction therefore refetches selected messages by `message_id`, compares
the authoritative `update_time` and content hash with the encrypted mirror, and
applies the same derived-data invalidation before an edited source is used.
Checkpoint reuse is conditional on an exact source-version match; a stale
checkpoint is discarded and rebuilt rather than failing the user task.

An eligible group message may be mirrored without starting an Agent run. This
observation path exists so a later explicit request can resolve a recently sent
image or file when Feishu delivers ordinary group messages to the application.
It applies the same enabled-chat, authorized-actor, membership, encryption,
retention, edit, recall, and revocation rules as task admission. It creates no
runtime event, task, acknowledgement, or model call.

A message starts an Agent run only when all conditions hold:

1. `chat_type` is `group`;
2. the sender is a user rather than a bot;
3. the mention list contains the current bot open ID;
4. the message ID has not already been admitted;
5. the chat and sender are still visible to the configured Feishu identity;
6. the normalized request contains non-empty user content after removing the bot
   mention placeholder.

Bot-authored messages, ordinary unaddressed chat, edited-event echoes, malformed
events, and duplicate deliveries do not invoke the model. Eligible ordinary
user messages may still take the observation path above; bot/system messages
and messages denied by chat, actor, or membership policy create no mirror row.

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
intent. An eligible ordinary message is ignored for task admission only after
its normalized encrypted mirror has been upserted.

The Channel adapter registers only message and bot-membership callbacks. A
callback converts the SDK value into the canonical inbound value and returns
only after the SQLite transaction containing event deduplication, task creation,
and acknowledgement intent commits. There is no process-local pre-admission
queue whose accepted contents could disappear on process exit. Concurrent SDK
callbacks may wait briefly on the single SQLite actor, but never call the model
or perform attachment parsing, context backfill, document, or Agent work.
Backpressure therefore remains at the upstream callback boundary.

The pinned Channel SDK normally schedules its public async handler after its
WebSocket dispatcher returns. The production adapter therefore binds at the
pinned P2 dispatcher method, converts the raw SDK model to the canonical value,
and submits the admission coroutine to the already-running Gateway loop with a
thread-safe future. The SDK dispatcher blocks on that future for at most 25
seconds, but no extra admission executor or per-event asyncio loop exists.
Concurrent callbacks may schedule concurrently; SQLite remains the single
transaction serializer. A committed admission returns normally; validation,
storage, cancellation, or timeout failure cancels the submitted coroutine and
propagates to the dispatcher so the WebSocket response is non-success and
Feishu can redeliver.

The official Channel object is created synchronously before the Gateway calls
`asyncio.run()`. This prevents the pinned SDK's module-level WebSocket loop from
capturing the active Runtime loop. Event callbacks cross from the SDK thread to
the Runtime only through the bounded bridge above; reconnect notifications use
the same Runtime loop's thread-safe scheduling. Async message sends and media
downloads remain ordinary `ChannelPort` calls from the Runtime loop. The pinned
adapter also binds SDK-owned periodic transport work to that WebSocket loop and
cancels it before stopping the loop. SDK background cancellation uses a
thread-safe callback barrier instead of creating a final coroutine during
shutdown, so graceful shutdown leaves no orphaned coroutines. This
version-coupled lifecycle is covered in a clean subprocess and must be
re-audited before changing `lark-channel-sdk`.

The IM outbox publisher accepts only typed acknowledgement, progress, approval,
and terminal payloads. It replies to the source `message_id`, preserves thread
placement, and derives a deterministic RFC 4122 UUID from the durable internal
idempotency key before calling Feishu. Internal keys remain descriptive and are
not constrained by transport field limits, while every retry maps to the same
valid Feishu UUID. An SDK result is sent only when success and an upstream
message reference are both present. Failed or ambiguous sends remain retryable
and are never converted into task completion.

### Active-run control admission

An exact mention in the active source thread is classified before task
creation. Only the requester who opened the active task can issue `/cancel`,
`/interrupt`, deterministic steering prefixes, or a follow-up for that run.
The event, encrypted control, trusted target task, and acknowledgement outbox
intent commit atomically before the long-connection callback returns. Other
participants cannot steer or cancel that work; their mentions follow normal
per-SessionKey ordering.

Control acknowledgement means only that the update is durable. It is not a
claim that an in-flight Feishu write was interrupted. The Harness consumes the
control at its next complete model/tool boundary, checkpoints applied control
IDs, and then acknowledges the inbox item. Duplicate Feishu delivery and
process restart must not duplicate the instruction.

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
evidence of completion. User-facing Feishu document references are labeled and
rendered as complete absolute HTTPS URLs so the client can make them clickable;
opaque document tokens are never presented as successful completion links.

Typed warning codes remain in durable outcomes and diagnostics, but the IM
renderer never exposes internal identifiers such as
`im_context_history_unavailable`. Known context warnings are localized and
deduplicated: missing history plus incomplete context becomes one concise note
that Feishu rejected bot-identity group-history access and the answer therefore
used only the verified current message. This warning maps to upstream error
`230027`; the operations guide names the required base message permission plus
the `im:message.group_msg` application-identity scope. Other warnings retain their existing truthful presentation until a
dedicated localization is defined.

Acknowledgement and terminal replies use deterministic idempotency keys derived
from the source `message_id` and reply kind.

After execution begins, one idempotent progress intent tells the requester that
authoritative group context is being collected and the Agent has started. It is
inserted before slow context/model/tool work, delivered after acknowledgement,
and never claims that an external write has completed. Retries reuse the same
key and cannot spam duplicate progress messages. Additional fine-grained tool
progress remains a future resource/plugin concern.

For one admitted request, the acknowledgement intent is always leased before
progress, approval, or terminal intents, including when their timestamps are
identical after a fast model response. Delivery retry preserves this causal
order so the user never sees “completed” before “received.”

Destructive-tool approval uses a Feishu interactive card. The production
Channel adapter handles `card.action.trigger` at the same synchronous durable
callback boundary as message admission. Only the task's originating requester
may decide; the runtime acknowledges approved, rejected, duplicate, expired, or
unauthorized decisions without exposing stored content.

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
- explicit attachment lookup (a request names a file or asks to collect/search
  group files): 500 preceding messages or 30 days;
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
history as complete. The trigger-message live read and trusted binding are
required. When Feishu explicitly reports that the bot lacks the optional group
history scope, the provider continues with the verified trigger only and emits
`im_context_history_unavailable`; it never substitutes stale mirror content.
Network failures, malformed responses, binding violations, and other live-read
errors still fail the run. Local mirror rows are used only for restart and
reconciliation.

The production provider also loads attachment references from the trigger and
selected contextual messages through the bounded attachment service. Downloads
are authorized by tenant/app/chat/message/resource binding and use the Feishu
message-resource endpoint. Parsed text or safe metadata becomes separately
attributed evidence; parser blocks, truncation, and unsupported media become
typed warnings rather than silent omissions.

Attachment resolution is relationship-first and must remain within IM:

1. use an attachment on the triggering message;
2. use an attachment on the replied-to message or active thread;
3. for an unthreaded request that names a file, resolve an exact normalized
   filename in the bounded 30-day/500-message attachment-search page;
4. if history is unauthorized, no exact match exists, or several exact matches
   are ambiguous, report the attachment as unavailable and ask the user to
   reply directly to the file message or attach it again.

The Agent must not search Drive, Docs, the managed authoring folder, or another
business-data source as a substitute for an unresolved group attachment. When
group-history listing is denied but Feishu previously delivered the ordinary
file event, the encrypted local attachment index may identify a unique exact
filename and its message ID inside the same bounded lookback window. The
provider must refetch that exact message ID from Feishu, revalidate tenant/app/
chat and filename bindings, and use only the live result. Local observation is
therefore a discovery index, never an authoritative content fallback. If the
event was not delivered, the live refetch fails, or the filename is ambiguous,
the attachment remains unavailable.

The expanded attachment window is selected deterministically before inference
when the normalized request contains an attachment/file intent or a recognizable
filename extension. It does not expose a free-form history query to the model.
Only attachments whose normalized filename occurs in the active request are
downloaded for analysis; asking to collect files does not automatically download
every binary in the group. The same 30-day bound applies to encrypted local
candidate discovery when application-identity history listing is unavailable.

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
| source code and scripts | bounded inert UTF-8 text decoding; never execute |
| archive and executable | store when policy permits; never unpack or execute automatically |

Every parser returns text or structured evidence with source provenance,
truncation warnings, parser version, and content hash. Parsing never executes
macros, embedded programs, formulas, links, or instructions found in the file.

### Runtime API 1 attachment ingest and parsers

The Runtime defaults to an inclusive 200 MiB (209,715,200-byte) limit per
downloaded attachment. Operators may set a different positive deployment limit
with `CODEX2LARK_MAX_ATTACHMENT_BYTES`; task policy may only reduce the active
deployment limit. A trustworthy declared size above the active limit is rejected
before download, and the actual downloaded byte count is checked again before
encrypted persistence or parsing. Unknown declared size does not bypass the
post-download check.

Parser limits remain independent from the transport limit: Office ZIP parsing
allows at most 50 MiB total declared uncompressed members, 1,000 ZIP entries, a
100:1 member compression-ratio ceiling, and 200,000 output characters. A file
may therefore download successfully but still yield bounded metadata or a
blocked/failed parse result without executing its contents.

Download authorization is a trusted `AttachmentLoadRequest` bound to tenant,
app, chat, message, and resource key. The repository must already contain that
reference. Downloaded plaintext exists only in memory for the bounded ingest;
the managed blob store writes an authenticated encrypted envelope through an
owner-only temporary file and atomic link. SQLite then records the blob
reference. A crash before that transaction may leave only an encrypted orphan,
which maintenance removes; a database row never points at plaintext.

Text/Markdown/JSON/CSV and recognized source-code/script extensions use bounded
UTF-8 decoding. Script text is evidence only: it is never imported, evaluated,
spawned, sourced, or passed to a shell. PDF uses the pinned
`pypdf` parser without following links. DOCX/XLSX/PPTX use bounded ZIP member
selection and `defusedxml`; formulas are rendered as inert text and never
evaluated. Images produce a typed managed-image reference plus metadata. Audio
and video produce metadata only. Archives and executables return a blocked
parser result and are never unpacked or executed. Parser output is
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
Garbage collection deletes checkpoints referencing an expiring message or its
attachments in the same transaction before deleting that source content.

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

### Durable hierarchical task scheduling

Every admitted task persists trusted `tenant_key`, `app_id`, and optional
`group_id` scheduling scope beside its encrypted payload. These values come
from the normalized event/admission adapter, never from message text, model
arguments, encrypted payload inspection, or parsing `SessionKey`.

One SQLite lease transaction enforces four uniform limits: global active root
tasks, active tasks per tenant, per tenant/application, and per
tenant/application/group. SessionKey serialization remains an additional hard
constraint. Selection is incremental: after each chosen task the transaction
updates its lease and provisional counts before choosing another, so one batch
cannot oversubscribe a parent scope.

Fairness uses durable least-recently-served cursors for tenant, application,
group, and SessionKey lanes. Candidate ordering compares those cursors in that
order, then uses priority, creation time, and task ID only as deterministic
tie-breakers. A noisy lane therefore cannot take a second turn while another
eligible never/less-recently served lane is waiting. Expired leases still count
until atomically reclaimed, and two concurrent lease requests serialize through
the database actor/transaction.

The single-node defaults are global `4`, tenant `4`, application `4`, and group
`2`. Limits must be positive and satisfy
`group <= application <= tenant <= global`. The worker batch size is only a
bounded fetch/execution size; it does not replace scheduling policy.

### Group-bound capability authority

A task admitted from a Feishu group receives its `chat_id` only through trusted
routing metadata. Group capability tools must derive their source scope from
that `ToolContext`; model arguments, user text, chat display names, delegated
task briefs, and document contents cannot select a different group.

Accordingly, the runtime `feishu.chat.digest.publish` schema exposes only the
time range and bounded rendering options. It does not expose `chat_id` or
`chat_name`. The tool injects the current trusted chat ID for execution and
write-target resolution. Delegation may reserve only the `current_chat` marker
or that same chat ID; both resolve to the trusted ID, while a missing binding or
cross-chat declaration is rejected before service access.
Interactive MCP publishing remains a separate, explicit user-authorized path
whose request may select a chat by exact ID or unambiguous name.

### Admission authorization policy

Admission applies a trusted operator policy before writing the incoming message
mirror, acknowledgement, task, rollout selection, or invoking a model. The
default single-node profile enables any group in which the bot has visible
membership and authorizes any non-bot human actor delivered by Feishu. Operators
may narrow this with comma-separated exact IDs:

- `CODEX2LARK_ENABLED_CHAT_IDS` permits only those chat IDs;
- `CODEX2LARK_AUTHORIZED_ACTOR_IDS` permits only those sender open IDs.

An empty variable means the corresponding default-open policy; whitespace,
empty list members, and duplicate IDs are normalized and rejected where
ambiguous. Existing `im_chats.enabled=0` or `access_state=revoked` always wins
over configuration. A previously unknown chat may be admitted under the
default-open policy and becomes a typed enabled mirror only after authorization.
Bot removal disables the chat immediately. Policy-denied events produce no
local message content, event, task, acknowledgement, rollout binding, or model
call. Repeated delivery remains denied deterministically.

The durable task scheduler leases at most one task per SessionKey and excludes
any SessionKey that already has a live lease. A batch may execute independent
SessionKeys concurrently. A handler returns a typed terminal task result and
its terminal reply intent commits in the same transaction. Transient provider
failures return the task to pending with bounded delay; retry exhaustion commits
a failed terminal reply rather than leaving an acknowledged request silent.
If a worker process dies while holding the final execution lease, lease recovery
does not mark the task failed directly. It schedules a durable finalization
lease. The next worker skips business execution, asks the registered handler to
render the typed failure outcome, and atomically commits that terminal state and
reply intent. Finalization itself is repeatable until that transaction commits.

The automated burst fixture admits at least 64 independent group SessionKeys
plus repeated work for noisy SessionKeys. Fixed-size lease batches must contain
at most one task per SessionKey, eventually lease every independent group, and
leave repeated same-session work pending until its predecessor completes. Every
leased request creates and terminally closes its own tenant/app-bound root graph;
the fixture asserts all graphs exist and no graph/source binding crosses groups.
This proves bounded scheduler progress, many-graph fairness, and session
isolation; it is not a throughput SLO or a substitute for opt-in live Feishu
soak testing.

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
