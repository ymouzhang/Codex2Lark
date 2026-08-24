from __future__ import annotations

SCHEMA_VERSION = 7

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

MULTI_AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_graphs (
    graph_id TEXT PRIMARY KEY,
    root_run_id TEXT NOT NULL UNIQUE,
    root_node_id TEXT NOT NULL UNIQUE,
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    source_resource_kind TEXT NOT NULL,
    source_resource_id TEXT NOT NULL,
    agent_definition_id TEXT NOT NULL,
    agent_definition_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    max_depth INTEGER NOT NULL,
    max_nodes INTEGER NOT NULL,
    max_concurrency INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_agent_nodes (
    node_id TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL,
    parent_node_id TEXT,
    canonical_path TEXT NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    task_brief_ciphertext BLOB NOT NULL,
    expected_output_type TEXT NOT NULL,
    context_mode TEXT NOT NULL,
    tool_ids_ciphertext BLOB NOT NULL,
    budget_ciphertext BLOB NOT NULL,
    deadline_ms INTEGER,
    depth INTEGER NOT NULL,
    status TEXT NOT NULL,
    lease_owner TEXT,
    lease_expires_at_ms INTEGER,
    attempt_count INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    UNIQUE (graph_id, canonical_path),
    UNIQUE (graph_id, parent_node_id, name),
    FOREIGN KEY (graph_id) REFERENCES runtime_graphs(graph_id) ON DELETE CASCADE,
    FOREIGN KEY (parent_node_id) REFERENCES runtime_agent_nodes(node_id)
);

CREATE INDEX IF NOT EXISTS runtime_agent_nodes_ready_idx
ON runtime_agent_nodes(graph_id, status, depth, created_at_ms);

CREATE TABLE IF NOT EXISTS runtime_agent_edges (
    graph_id TEXT NOT NULL,
    predecessor_node_id TEXT NOT NULL,
    dependent_node_id TEXT NOT NULL,
    edge_kind TEXT NOT NULL,
    PRIMARY KEY (graph_id, predecessor_node_id, dependent_node_id, edge_kind),
    FOREIGN KEY (graph_id) REFERENCES runtime_graphs(graph_id) ON DELETE CASCADE,
    FOREIGN KEY (predecessor_node_id) REFERENCES runtime_agent_nodes(node_id),
    FOREIGN KEY (dependent_node_id) REFERENCES runtime_agent_nodes(node_id)
);

CREATE TABLE IF NOT EXISTS runtime_mailbox (
    item_id TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL,
    sender_node_id TEXT NOT NULL,
    recipient_node_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    correlation_id TEXT,
    sequence INTEGER NOT NULL,
    payload_ciphertext BLOB NOT NULL,
    state TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    delivered_at_ms INTEGER,
    acknowledged_at_ms INTEGER,
    UNIQUE (recipient_node_id, sequence),
    FOREIGN KEY (graph_id) REFERENCES runtime_graphs(graph_id) ON DELETE CASCADE,
    FOREIGN KEY (sender_node_id) REFERENCES runtime_agent_nodes(node_id),
    FOREIGN KEY (recipient_node_id) REFERENCES runtime_agent_nodes(node_id)
);

CREATE INDEX IF NOT EXISTS runtime_mailbox_recipient_idx
ON runtime_mailbox(recipient_node_id, state, sequence);

CREATE TABLE IF NOT EXISTS runtime_artifacts (
    artifact_id TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL,
    producer_node_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    payload_ciphertext BLOB NOT NULL,
    source_versions_ciphertext BLOB NOT NULL,
    verification_state TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    expires_at_ms INTEGER,
    created_at_ms INTEGER NOT NULL,
    FOREIGN KEY (graph_id) REFERENCES runtime_graphs(graph_id) ON DELETE CASCADE,
    FOREIGN KEY (producer_node_id) REFERENCES runtime_agent_nodes(node_id)
);

CREATE TABLE IF NOT EXISTS runtime_agent_checkpoints (
    checkpoint_id TEXT PRIMARY KEY,
    graph_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    state_ciphertext BLOB NOT NULL,
    created_at_ms INTEGER NOT NULL,
    UNIQUE (node_id, sequence),
    FOREIGN KEY (graph_id) REFERENCES runtime_graphs(graph_id) ON DELETE CASCADE,
    FOREIGN KEY (node_id) REFERENCES runtime_agent_nodes(node_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS runtime_resource_locks (
    tenant_key TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    graph_id TEXT NOT NULL,
    owner_node_id TEXT NOT NULL,
    expected_revision TEXT,
    lease_expires_at_ms INTEGER NOT NULL,
    created_at_ms INTEGER NOT NULL,
    PRIMARY KEY (tenant_key, resource_type, resource_id),
    FOREIGN KEY (graph_id) REFERENCES runtime_graphs(graph_id) ON DELETE CASCADE,
    FOREIGN KEY (owner_node_id) REFERENCES runtime_agent_nodes(node_id)
);

CREATE TABLE IF NOT EXISTS runtime_budget_ledger (
    graph_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    budget_kind TEXT NOT NULL,
    maximum INTEGER NOT NULL,
    reserved INTEGER NOT NULL,
    consumed INTEGER NOT NULL,
    updated_at_ms INTEGER NOT NULL,
    PRIMARY KEY (graph_id, node_id, budget_kind),
    FOREIGN KEY (graph_id) REFERENCES runtime_graphs(graph_id) ON DELETE CASCADE,
    FOREIGN KEY (node_id) REFERENCES runtime_agent_nodes(node_id)
);
"""

IM_SCHEMA = """
CREATE TABLE IF NOT EXISTS im_chats (
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    name_ciphertext BLOB,
    chat_mode TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    bot_member_state TEXT NOT NULL,
    access_state TEXT NOT NULL,
    last_reconciled_at_ms INTEGER NOT NULL,
    retention_policy_id TEXT NOT NULL,
    purge_after_ms INTEGER,
    PRIMARY KEY (tenant_key, app_id, chat_id)
);

CREATE TABLE IF NOT EXISTS im_messages (
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    thread_id TEXT,
    root_id TEXT,
    parent_id TEXT,
    sender_type TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    sender_name_ciphertext BLOB,
    message_type TEXT NOT NULL,
    content_ciphertext BLOB NOT NULL,
    mentions_ciphertext BLOB NOT NULL,
    content_hash TEXT NOT NULL,
    created_at_source_ms INTEGER NOT NULL,
    updated_at_source_ms INTEGER NOT NULL,
    is_recalled INTEGER NOT NULL,
    is_deleted INTEGER NOT NULL,
    schema_version INTEGER NOT NULL,
    last_reconciled_at_ms INTEGER NOT NULL,
    expires_at_ms INTEGER,
    PRIMARY KEY (tenant_key, app_id, message_id),
    FOREIGN KEY (tenant_key, app_id, chat_id)
      REFERENCES im_chats(tenant_key, app_id, chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS im_messages_context_idx
ON im_messages(tenant_key, app_id, chat_id, created_at_source_ms, message_id);

CREATE TABLE IF NOT EXISTS im_attachments (
    tenant_key TEXT NOT NULL,
    app_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    resource_key TEXT NOT NULL,
    chat_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    filename_ciphertext BLOB,
    media_type TEXT,
    declared_size INTEGER,
    blob_id TEXT,
    download_state TEXT NOT NULL,
    parse_state TEXT NOT NULL,
    parser_id TEXT,
    parser_version TEXT,
    parsed_content_ciphertext BLOB,
    parsed_content_hash TEXT,
    warning_code TEXT,
    expires_at_ms INTEGER,
    PRIMARY KEY (tenant_key, app_id, message_id, resource_key),
    FOREIGN KEY (tenant_key, app_id, message_id)
      REFERENCES im_messages(tenant_key, app_id, message_id) ON DELETE CASCADE
);
"""

IM_BLOB_SCHEMA = """
ALTER TABLE im_attachments ADD COLUMN parsing_policy_version TEXT;

CREATE TABLE IF NOT EXISTS im_file_blobs (
    blob_id TEXT PRIMARY KEY,
    byte_size INTEGER NOT NULL,
    media_type TEXT,
    created_at_ms INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS im_attachments_blob_idx
ON im_attachments(blob_id);
"""

RUN_CONTROL_SCHEMA = """
CREATE TABLE IF NOT EXISTS runtime_run_controls (
    control_id TEXT PRIMARY KEY,
    event_pk INTEGER NOT NULL UNIQUE,
    target_task_id TEXT NOT NULL,
    session_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload_ciphertext BLOB NOT NULL,
    state TEXT NOT NULL,
    created_at_ms INTEGER NOT NULL,
    applied_at_ms INTEGER,
    FOREIGN KEY (event_pk) REFERENCES runtime_events(event_pk),
    FOREIGN KEY (target_task_id) REFERENCES runtime_tasks(task_id)
);

CREATE INDEX IF NOT EXISTS runtime_run_controls_target_idx
ON runtime_run_controls(target_task_id, state, created_at_ms, control_id);
"""

AGENT_WRITE_SCOPE_SCHEMA = """
ALTER TABLE runtime_agent_nodes
ADD COLUMN requires_write_scope INTEGER NOT NULL DEFAULT 0;
"""

MIGRATIONS: tuple[tuple[int, str], ...] = (
    (1, INITIAL_SCHEMA),
    (2, SESSION_SCHEMA),
    (3, MULTI_AGENT_SCHEMA),
    (4, IM_SCHEMA),
    (5, IM_BLOB_SCHEMA),
    (6, RUN_CONTROL_SCHEMA),
    (7, AGENT_WRITE_SCOPE_SCHEMA),
)
