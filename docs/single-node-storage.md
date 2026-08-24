# Single-node storage and recovery

## 1. Decision

The default production deployment is one machine running one Codex2Lark
Runtime process. SQLite provides durable coordination and typed metadata. A
managed local directory stores encrypted attachment blobs. No external database,
message broker, cache, or object store is required.

Feishu remains the upstream source of truth. Local content is an authorized,
rebuildable mirror used for restart recovery, bounded context, file parsing,
deduplication, and performance. A local row never grants access that live Feishu
or trusted policy does not grant.

## 2. Data directory

The operator configures a data directory or accepts the operating system's
application-data location. It must not be inside the source repository.

```text
data-dir/
├── runtime.db
├── runtime.db-wal
├── runtime.db-shm
├── blobs/
│   └── <hash-prefix>/<encrypted-content-hash>.blob
├── tmp/
└── backups/
```

`tmp/` contains request workspaces and is swept on startup. Durable blob names
reveal only a keyed or encrypted identifier, never the original filename,
tenant, chat, sender, or document title.

## 3. SQLite profile

The storage adapter uses the Python standard-library SQLite driver, parameterized
SQL, explicit transactions, foreign keys, WAL mode, and a busy timeout. One
process owns migrations and periodic maintenance. A bounded connection pool is
unnecessary in the first release. One dedicated database Actor thread owns the
connection for its complete lifetime. Async callers submit typed repository
operations through a bounded request queue and await a result queue, so SQLite
work is serialized without blocking the event loop or moving a live connection
between threads. Shutdown drains accepted operations before the Actor
checkpoints and closes the connection.

Required startup checks are:

- data directory ownership and permissions;
- SQLite version and required features;
- schema compatibility and migration checksum;
- free disk and configured high-water mark;
- encryption key availability and key identifier;
- blob/database consistency sample;
- ability to create, commit, roll back, checkpoint, and delete a probe record.

## 4. Common schema

### `runtime_events`

Stores normalized delivery and optional encrypted source payload:

```text
event_id primary key
plugin_id
event_type
tenant_key
app_id
occurred_at
received_at
schema_version
resource_kind
resource_id
correlation_id
payload_ciphertext nullable
payload_expires_at nullable
status
created_at
```

The `(tenant_key, app_id, event_id)` identity is unique. A duplicate delivery
updates delivery diagnostics but does not create a second command.

### `runtime_tasks`

Implements the durable queue:

```text
task_id primary key
event_id nullable
plugin_id
command_type
session_key
priority
payload_ciphertext
state
available_at
lease_owner nullable
lease_expires_at nullable
attempt_count
max_attempts
last_error_code nullable
created_at
updated_at
```

Allowed states are `pending`, `leased`, `succeeded`, `blocked`, `failed`, and
`cancelled`. A transaction selects eligible work, assigns a lease, and records
the attempt. Startup recovery returns expired leases to `pending` unless the
retry budget is exhausted. An exhausted execution lease also returns to
`pending`, tagged for terminal finalization; it is never changed directly to
`failed`. A finalization lease does not consume another business-execution
attempt and may only render and atomically commit the handler's failure outcome
and terminal outbox intent.

### `runtime_runs` and `runtime_run_events`

`runtime_runs` stores content-minimized run identity, bindings, policy versions,
budgets, status, and terminal verification. `runtime_run_events` stores ordered
typed lifecycle events. Model reasoning and unrestricted prompts are not stored.
Plugin policy may retain encrypted user-visible messages, tool arguments, and
observations when they are required for recovery; each field has a purpose and
TTL.

### `runtime_outbox`

External reply intent commits in the same transaction as the corresponding run
transition:

```text
outbox_id primary key
run_id
publisher_id
destination_ref
message_kind
idempotency_key unique
payload_ciphertext
state
available_at
attempt_count
last_error_code nullable
created_at
updated_at
```

An outbox worker publishes after commit and records the upstream result. This
prevents a successful transaction with a forgotten reply intent. It does not
claim exactly-once upstream delivery; the publisher uses the stable idempotency
key and verifies the observable result where supported.

### `runtime_idempotency`

Stores bounded operation keys, states, result references, and expiry. It never
stores a full response body solely for convenience.

### Multi-Agent coordination

The kernel additionally owns typed `runtime_graphs`, `runtime_agent_nodes`,
`runtime_agent_edges`, `runtime_mailbox`, `runtime_artifacts`,
`runtime_resource_locks`, `runtime_agent_checkpoints`, and
`runtime_budget_ledger` tables. They persist graph topology, scoped assignments,
lifecycle, mailbox delivery, typed artifact references, write exclusion,
recovery state, and reserved/consumed budgets. They do not persist hidden model
reasoning. The field-level contract is defined in
[multi-agent-runtime.md](multi-agent-runtime.md).

## 5. Plugin schema

Plugins use typed tables in an assigned namespace. The IM plugin initially owns:

### `im_chats`

```text
tenant_key + app_id + chat_id primary key
name
chat_mode
enabled
bot_member_state
last_reconciled_at
retention_policy_id
access_state
purge_after nullable
```

### `im_messages`

```text
tenant_key + app_id + message_id primary key
chat_id
thread_id nullable
root_id nullable
parent_id nullable
sender_type
sender_id
sender_name nullable
message_type
content_ciphertext
content_hash
created_at_source
updated_at_source nullable
is_recalled
is_deleted
schema_version
last_reconciled_at
expires_at nullable
```

### `im_attachments`

```text
tenant_key + app_id + message_id + resource_key primary key
chat_id
resource_type
filename_ciphertext nullable
media_type nullable
declared_size nullable
blob_id nullable
download_state
parse_state
parser_id nullable
parser_version nullable
parsed_content_ciphertext nullable
parsed_content_hash nullable
warning_code nullable
expires_at nullable
```

An attachment row may exist without downloaded bytes. Blob deletion occurs only
after no live attachment row references it.

## 6. Blob encryption

Message content, task payloads, outbox payloads, stored context, filenames, raw
events, parsed content, and attachment bytes are business data and are encrypted
at rest.

The first release uses authenticated envelope encryption:

1. an external master key or key-encryption key is provided by an OS keychain,
   environment secret, or mounted secret file;
2. each blob or encrypted record receives a random data key and nonce;
3. data is encrypted with an authenticated cipher;
4. the data key is wrapped by the master key;
5. associated data binds ciphertext to tenant, plugin, table/resource identity,
   schema version, and key version.

Plaintext keys never enter SQLite, logs, model context, or error output. Key
rotation rewrites wrapped data keys without requiring immediate re-encryption of
every large blob. Removing the external master key renders backups unreadable,
so key backup is an explicit operator responsibility.

The first implementation uses AES-256-GCM from `cryptography`. The operator
provides a base64-encoded 32-byte master key and a non-secret key identifier.
Production configuration reads them from an external secret binding; the
initial local binding uses `CODEX2LARK_MASTER_KEY` and
`CODEX2LARK_MASTER_KEY_ID`. Each encrypted value has its own random 256-bit data
key and 96-bit nonce. The data key is wrapped with the master key using a
separate random nonce. The serialized envelope is versioned and contains only
algorithm, key ID, nonces, wrapped data key, and ciphertext.

The data directory and blob directory use owner-only permissions. New database,
temporary, and encrypted blob files use mode `0600`; directories use `0700`
subject to stricter operating-system policy.

Development may use an ephemeral generated key only with an ephemeral data
directory. Durable business-data persistence refuses to start without an
explicit external key.

## 7. Attachment ingest

Attachment ingest uses a staged commit:

1. create or update metadata as `pending`;
2. download into a request-local temporary file with byte and time limits;
3. validate actual size and sniffed media type;
4. calculate content hash while streaming;
5. encrypt into a new temporary blob;
6. atomically move the encrypted blob into the managed store;
7. commit the `file_blobs` reference and attachment state together;
8. delete plaintext temporary bytes in `finally`.

A crash before step 7 leaves an unreferenced encrypted temporary blob, removed
by startup sweeping. A crash after step 7 leaves a valid durable reference.
Files are never executed, mounted, served publicly, or extracted outside a
bounded request workspace.

## 8. Parser cache

Parser output is keyed by:

```text
content_hash + parser_id + parser_version + parsing_policy_version
```

Changing the parser or policy creates a new derived result; it does not silently
reuse incompatible text. Results contain provenance and explicit truncation.
Deleting the source attachment invalidates all derived results unless another
authorized live attachment references the same blob and policy permits reuse.

## 9. Retention

Retention policy is versioned and can be bound globally or per resource. Initial
conservative defaults are:

| Data | Default TTL |
|---|---|
| normalized IM message content | 90 days |
| attachment bytes and parser output | 30 days |
| encrypted raw event payload | 7 days when enabled |
| completed run recovery data | 30 days |
| context checkpoints | 90 days |
| idempotency and delivery diagnostics | 7 days |

`forever` is allowed only by explicit operator policy. `disabled` stores only
the minimum operational data necessary to reject duplicates and execute the
current task.

Garbage collection runs in bounded batches and follows this order:

1. mark expired or administratively deleted source rows;
2. invalidate dependent context and parser rows;
3. delete expired ciphertext rows;
4. remove unreferenced encrypted blobs;
5. checkpoint WAL when safe;
6. emit content-free counts and bytes reclaimed.

## 10. Reconciliation

Local data is never trusted solely because it exists. Before a sensitive read or
external write, the capability plugin checks live authorization and refetches
the relevant resource when its freshness policy requires it.

IM reconciliation handles:

- message edits by comparing source update time and content hash;
- recalls and deletions with tombstones and dependent-data invalidation;
- pagination gaps with an incomplete marker and resumable cursor;
- bot removal or chat access loss by disabling retrieval immediately;
- retention expiry independently of Feishu deletion;
- attachment resource failure without corrupting the parent message.

Local tombstones prevent an older redelivery or backfill page from resurrecting
deleted content without a newer trusted source version.

## 11. Context checkpoints

Persistent context is derived data, not an unrestricted transcript. A checkpoint
stores:

- SessionKey and AgentDefinition version;
- source message/resource IDs and their content versions;
- concise task intent and acceptance criteria;
- completed verified operations and result references;
- unresolved blockers and next action;
- token accounting, compactor version, and expiry.

It excludes hidden model reasoning and secrets. Any source edit, recall,
deletion, access loss, policy change, or plugin incompatibility invalidates the
affected checkpoint before reuse.

## 12. Disk protection

Storage configuration includes:

- maximum total managed bytes;
- warning and hard high-water percentages;
- maximum single attachment bytes;
- maximum bytes downloaded per run;
- maximum parser output bytes and tokens;
- minimum free bytes reserved for SQLite and shutdown.

At warning level, garbage collection runs early. At the hard level, new file
downloads and backfills stop, while event admission, text-only processing,
terminal replies, retention deletion, and operator diagnostics continue.

## 13. Backup and restore

A valid backup contains a consistent SQLite snapshot, every referenced encrypted
blob, schema/plugin version manifest, and the corresponding external encryption
key backup. Copying a live database file without its WAL is not a backup.

The supported backup flow uses SQLite's online backup mechanism, then copies the
immutable encrypted blobs referenced by that snapshot. Restore runs compatibility
and integrity checks before the Gateway starts event sources.

Because Feishu is upstream truth, losing a backup may allow message and resource
backfill within Feishu API limits, but queued task state, run checkpoints, and
expired upstream content may be unrecoverable.

## 14. Administrative operations

The CLI must eventually provide bounded operator commands for:

- storage status and integrity check;
- list schema and plugin migration versions;
- run retention garbage collection;
- purge one tenant, chat, message, run, or all business data;
- rotate the wrapping key;
- create and verify a backup;
- restore into an empty data directory;
- reconcile one chat or resume an incomplete backfill.

Destructive commands resolve exact targets, show counts and byte estimates, and
require explicit confirmation. Purge writes a content-free audit record but does
not retain deleted business content.

## 15. Failure guarantees

The single-node profile guarantees:

- committed tasks survive process restart;
- expired task leases are recoverable;
- reply intents survive process restart;
- duplicate events do not create duplicate commands;
- plugin migrations are checked before admission;
- local ciphertext cannot be interpreted without the external key;
- bounded cleanup handles success, failure, cancellation, timeout, and startup.

It does not guarantee availability after host, disk, or key loss; uninterrupted
operation during backup; or exactly-once external effects. Those limitations are
accepted for the chosen single-machine deployment.
