from __future__ import annotations

SCHEMA_VERSION = 2

INITIAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_plugins (
    plugin_id TEXT PRIMARY KEY,
    plugin_version TEXT NOT NULL,
    runtime_api INTEGER NOT NULL,
    state TEXT NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_events (
    event_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    plugin_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    occurred_at_ms INTEGER NOT NULL,
    received_at_ms INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    resource_kind TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    correlation_id TEXT,
    trace_id TEXT NOT NULL,
    payload_ciphertext BLOB,
    payload_expires_at_ms INTEGER,
    status TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (tenant_key, app_id, event_id)
);

CREATE TABLE IF NOT EXISTS runtime_tasks (
    task_id TEXT PRIMARY KEY,
    event_pk INTEGER,
    plugin_id TEXT NOT NULL,
    command_type TEXT NOT NULL,
    session_key TEXT NOT NULL,
    priority INTEGER NOT NULL,
    payload_ciphertext BLOB NOT NULL,
    state TEXT NOT NULL,
    available_at_ms INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expires_at_ms INTEGER,
    attempt_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    last_error_code TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY (event_pk) REFERENCES runtime_events(event_pk)
);

CREATE UNIQUE INDEX IF NOT EXISTS runtime_tasks_event_idx
ON runtime_tasks(event_pk) WHERE event_pk IS NOT NULL;

CREATE INDEX IF NOT EXISTS runtime_tasks_available_idx
ON runtime_tasks(state, available_at_ms, priority DESC, created_at_ms);

CREATE TABLE IF NOT EXISTS runtime_runs (
    run_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    agent_definition_id TEXT NOT NULL,
    agent_definition_version INTEGER NOT NULL,
    policy_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY (task_id) REFERENCES runtime_tasks(task_id)
);

CREATE TABLE IF NOT EXISTS runtime_run_events (
    run_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    payload_ciphertext BLOB,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (run_id, sequence),
    FOREIGN KEY (run_id) REFERENCES runtime_runs(run_id)
);

CREATE TABLE IF NOT EXISTS runtime_outbox (
    outbox_id TEXT PRIMARY KEY,
    run_id TEXT,
    task_id TEXT,
    publisher_id TEXT NOT NULL,
    destination_ref TEXT NOT NULL,
    message_kind TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_ciphertext BLOB NOT NULL,
    state TEXT NOT NULL,
    available_at_ms INTEGER NOT NULL,
    lease_owner TEXT,
    lease_expires_at_ms INTEGER,
    attempt_count INTEGER NOT NULL,
    max_attempts INTEGER NOT NULL,
    last_error_code TEXT,
    upstream_ref TEXT,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runtime_runs(run_id),
    FOREIGN KEY (task_id) REFERENCES runtime_tasks(task_id)
);

CREATE INDEX IF NOT EXISTS runtime_outbox_available_idx
ON runtime_outbox(state, available_at_ms, created_at_ms);

CREATE TABLE IF NOT EXISTS runtime_idempotency (
    idempotency_key TEXT PRIMARY KEY,
    operation_kind TEXT NOT NULL,
    state TEXT NOT NULL,
    owner TEXT NOT NULL,
    result_ref TEXT,
    expires_at_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);
"""

SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_checkpoints (
    run_id TEXT PRIMARY KEY,
    payload_ciphertext BLOB NOT NULL,
    next_turn INTEGER NOT NULL,
    agent_id TEXT NOT NULL,
    agent_version INTEGER NOT NULL,
    compactor_version INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runtime_runs(run_id) ON DELETE CASCADE
);
"""

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, INITIAL_SCHEMA),
    (2, SESSION_SCHEMA),
)
