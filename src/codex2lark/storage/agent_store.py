from __future__ import annotations

import json
import sqlite3
from typing import Any
from uuid import uuid4

from codex2lark.runtime.multi_agent import (
    AgentCheckpoint,
    AgentNode,
    AgentRole,
    Artifact,
    ArtifactDraft,
    ContextMode,
    GraphLimits,
    GraphRecord,
    GraphStatus,
    MailboxItem,
    MailboxKind,
    MailboxState,
    NodeSpec,
    NodeStatus,
    ResourceTarget,
)
from codex2lark.runtime.types import VerificationState

from .crypto import EnvelopeCipher
from .database import SQLiteDatabase


class SQLiteAgentGraphStore:
    def __init__(self, database: SQLiteDatabase, cipher: EnvelopeCipher) -> None:
        self._database = database
        self._cipher = cipher

    async def create_graph(
        self,
        *,
        graph_id: str,
        root_run_id: str,
        tenant_key: str,
        app_id: str,
        source_resource_kind: str,
        source_resource_id: str,
        agent_definition_id: str,
        agent_definition_version: int,
        root_spec: NodeSpec,
        limits: GraphLimits,
        now_ms: int,
    ) -> tuple[GraphRecord, AgentNode]:
        root_node_id = str(uuid4())

        def operation(connection: sqlite3.Connection) -> tuple[GraphRecord, AgentNode]:
            connection.execute(
                """
                INSERT INTO runtime_graphs(
                    graph_id, root_run_id, root_node_id, tenant_key, app_id,
                    source_resource_kind, source_resource_id, agent_definition_id,
                    agent_definition_version, status, max_depth, max_nodes,
                    max_concurrency, created_at_ms, updated_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, ?, ?, ?)
                """,
                (
                    graph_id,
                    root_run_id,
                    root_node_id,
                    tenant_key,
                    app_id,
                    source_resource_kind,
                    source_resource_id,
                    agent_definition_id,
                    agent_definition_version,
                    limits.max_depth,
                    limits.max_nodes,
                    limits.max_concurrency,
                    now_ms,
                    now_ms,
                ),
            )
            self._insert_node(
                connection,
                node_id=root_node_id,
                graph_id=graph_id,
                parent_node_id=None,
                canonical_path="/root",
                spec=root_spec,
                depth=1,
                now_ms=now_ms,
            )
            graph = GraphRecord(
                graph_id=graph_id,
                root_run_id=root_run_id,
                root_node_id=root_node_id,
                tenant_key=tenant_key,
                app_id=app_id,
                source_resource_kind=source_resource_kind,
                source_resource_id=source_resource_id,
                agent_definition_id=agent_definition_id,
                agent_definition_version=agent_definition_version,
                status=GraphStatus.ACTIVE,
                limits=limits,
            )
            return graph, AgentNode(
                root_node_id,
                graph_id,
                None,
                "/root",
                root_spec,
                1,
                NodeStatus.READY,
            )

        return await self._database.transaction(operation)

    async def get_graph(self, graph_id: str) -> GraphRecord:
        row = await self._database.call(
            lambda connection: connection.execute(
                "SELECT * FROM runtime_graphs WHERE graph_id = ?", (graph_id,)
            ).fetchone()
        )
        if row is None:
            raise LookupError(f"graph does not exist: {graph_id}")
        return self._graph(row)

    async def get_node(self, node_id: str) -> AgentNode:
        def operation(connection: sqlite3.Connection) -> tuple[sqlite3.Row | None, tuple[str, ...]]:
            row = connection.execute(
                "SELECT * FROM runtime_agent_nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            return row, self._dependencies(connection, node_id)

        row, dependencies = await self._database.call(operation)
        if row is None:
            raise LookupError(f"Agent node does not exist: {node_id}")
        return self._node(row, dependencies)

    async def list_nodes(self, graph_id: str) -> list[AgentNode]:
        def operation(
            connection: sqlite3.Connection,
        ) -> tuple[list[sqlite3.Row], dict[str, tuple[str, ...]]]:
            rows = connection.execute(
                """
                SELECT * FROM runtime_agent_nodes
                WHERE graph_id = ? ORDER BY depth, canonical_path
                """,
                (graph_id,),
            ).fetchall()
            return rows, {
                row["node_id"]: self._dependencies(connection, row["node_id"]) for row in rows
            }

        rows, dependencies = await self._database.call(operation)
        return [self._node(row, dependencies[row["node_id"]]) for row in rows]

    async def spawn_child(
        self, graph_id: str, parent_node_id: str, spec: NodeSpec, *, now_ms: int
    ) -> AgentNode:
        node_id = str(uuid4())

        def operation(connection: sqlite3.Connection) -> AgentNode:
            graph_row = connection.execute(
                "SELECT * FROM runtime_graphs WHERE graph_id = ?", (graph_id,)
            ).fetchone()
            if graph_row is None or graph_row["status"] != GraphStatus.ACTIVE.value:
                raise RuntimeError("graph is not active")
            parent = connection.execute(
                "SELECT * FROM runtime_agent_nodes WHERE node_id = ? AND graph_id = ?",
                (parent_node_id, graph_id),
            ).fetchone()
            if parent is None:
                raise LookupError("parent node does not exist in graph")
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM runtime_agent_nodes WHERE graph_id = ?", (graph_id,)
                ).fetchone()[0]
            )
            if count >= graph_row["max_nodes"]:
                raise ValueError("graph node limit exceeded")
            depth = int(parent["depth"]) + 1
            if depth > graph_row["max_depth"]:
                raise ValueError("graph depth limit exceeded")
            for dependency_id in spec.dependency_node_ids:
                dependency = connection.execute(
                    "SELECT 1 FROM runtime_agent_nodes WHERE graph_id = ? AND node_id = ?",
                    (graph_id, dependency_id),
                ).fetchone()
                if dependency is None:
                    raise ValueError("dependency does not belong to graph")
            if parent["status"] in (NodeStatus.READY.value, NodeStatus.INTERRUPTED.value):
                connection.execute(
                    """
                    UPDATE runtime_agent_nodes SET status = 'waiting', updated_at_ms = ?
                    WHERE node_id = ?
                    """,
                    (now_ms, parent_node_id),
                )
            self._reserve_parent_budget(
                connection, graph_id, parent_node_id, spec.budgets, now_ms=now_ms
            )
            path = f"{parent['canonical_path']}/{spec.name}"
            self._insert_node(
                connection,
                node_id=node_id,
                graph_id=graph_id,
                parent_node_id=parent_node_id,
                canonical_path=path,
                spec=spec,
                depth=depth,
                now_ms=now_ms,
            )
            for dependency_id in spec.dependency_node_ids:
                connection.execute(
                    """
                    INSERT INTO runtime_agent_edges(
                        graph_id, predecessor_node_id, dependent_node_id, edge_kind
                    ) VALUES (?, ?, ?, 'depends_on')
                    """,
                    (graph_id, dependency_id, node_id),
                )
            return AgentNode(
                node_id,
                graph_id,
                parent_node_id,
                path,
                spec,
                depth,
                NodeStatus.READY,
            )

        return await self._database.transaction(operation)

    async def lease_ready(
        self,
        graph_id: str,
        *,
        worker_id: str,
        now_ms: int,
        lease_ms: int,
        limit: int,
    ) -> list[AgentNode]:
        if lease_ms < 1 or limit < 1 or not worker_id:
            raise ValueError("worker, lease, and limit must be positive")

        def operation(connection: sqlite3.Connection) -> list[AgentNode]:
            connection.execute(
                """
                UPDATE runtime_agent_nodes
                SET status = 'ready', lease_owner = NULL, lease_expires_at_ms = NULL,
                    updated_at_ms = ?
                WHERE graph_id = ? AND status = 'running' AND lease_expires_at_ms <= ?
                """,
                (now_ms, graph_id, now_ms),
            )
            connection.execute(
                "DELETE FROM runtime_resource_locks WHERE lease_expires_at_ms <= ?", (now_ms,)
            )
            graph = connection.execute(
                "SELECT status, max_concurrency FROM runtime_graphs WHERE graph_id = ?",
                (graph_id,),
            ).fetchone()
            if graph is None or graph["status"] != GraphStatus.ACTIVE.value:
                return []
            active = int(
                connection.execute(
                    """
                    SELECT COUNT(*) FROM runtime_agent_nodes
                    WHERE graph_id = ? AND status = 'running'
                    """,
                    (graph_id,),
                ).fetchone()[0]
            )
            capacity = max(0, min(limit, int(graph["max_concurrency"]) - active))
            if capacity == 0:
                return []
            rows = connection.execute(
                """
                SELECT n.* FROM runtime_agent_nodes n
                WHERE n.graph_id = ? AND n.status IN ('ready', 'interrupted')
                  AND (n.deadline_ms IS NULL OR n.deadline_ms > ?)
                  AND NOT EXISTS (
                      SELECT 1 FROM runtime_agent_nodes child
                      WHERE child.parent_node_id = n.node_id
                        AND child.status NOT IN ('completed', 'blocked', 'failed', 'cancelled')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM runtime_agent_edges e
                      JOIN runtime_agent_nodes p ON p.node_id = e.predecessor_node_id
                      WHERE e.graph_id = n.graph_id AND e.dependent_node_id = n.node_id
                        AND e.edge_kind = 'depends_on' AND p.status != 'completed'
                  )
                ORDER BY n.depth DESC, n.created_at_ms, n.node_id
                LIMIT ?
                """,
                (graph_id, now_ms, capacity),
            ).fetchall()
            expiry = now_ms + lease_ms
            leased: list[AgentNode] = []
            for row in rows:
                connection.execute(
                    """
                    UPDATE runtime_agent_nodes
                    SET status = 'running', lease_owner = ?, lease_expires_at_ms = ?,
                        attempt_count = attempt_count + 1, updated_at_ms = ?
                    WHERE node_id = ?
                    """,
                    (worker_id, expiry, now_ms, row["node_id"]),
                )
                leased.append(
                    self._node_with_lease(
                        row,
                        dependency_node_ids=self._dependencies(connection, row["node_id"]),
                        worker_id=worker_id,
                        lease_expires_at_ms=expiry,
                    )
                )
            return leased

        return await self._database.transaction(operation)

    async def fail_node(self, node_id: str, *, worker_id: str, now_ms: int) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            node = connection.execute(
                """
                SELECT * FROM runtime_agent_nodes
                WHERE node_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (node_id, worker_id),
            ).fetchone()
            if node is None:
                raise RuntimeError("Agent node lease is not owned by worker")
            connection.execute(
                """
                UPDATE runtime_agent_nodes
                SET status = 'failed', lease_owner = NULL, lease_expires_at_ms = NULL,
                    updated_at_ms = ? WHERE node_id = ?
                """,
                (now_ms, node_id),
            )
            connection.execute(
                "DELETE FROM runtime_resource_locks WHERE owner_node_id = ?", (node_id,)
            )
            self._release_parent_budget(connection, node, now_ms=now_ms)
            self._block_dependents(connection, node_id, now_ms=now_ms)
            self._wake_parent(connection, node["parent_node_id"], now_ms=now_ms)

        await self._database.transaction(operation)

    async def interrupt_node(self, node_id: str, *, now_ms: int) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE runtime_agent_nodes
                SET status = 'interrupted', lease_owner = NULL, lease_expires_at_ms = NULL,
                    updated_at_ms = ?
                WHERE node_id = ? AND status IN ('ready', 'running', 'waiting')
                """,
                (now_ms, node_id),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("Agent node is not interruptible")
            connection.execute(
                "DELETE FROM runtime_resource_locks WHERE owner_node_id = ?", (node_id,)
            )

        await self._database.transaction(operation)

    async def complete_node(
        self,
        node_id: str,
        *,
        worker_id: str,
        artifact: ArtifactDraft,
        now_ms: int,
    ) -> Artifact:
        artifact_id = str(uuid4())

        def operation(connection: sqlite3.Connection) -> Artifact:
            node = connection.execute(
                """
                SELECT * FROM runtime_agent_nodes
                WHERE node_id = ? AND status = 'running' AND lease_owner = ?
                """,
                (node_id, worker_id),
            ).fetchone()
            if node is None:
                raise RuntimeError("Agent node lease is not owned by worker")
            payload = self._encrypt_json(
                artifact.payload, aad=self._artifact_aad(artifact_id, "payload")
            )
            versions = self._encrypt_json(
                artifact.source_versions, aad=self._artifact_aad(artifact_id, "sources")
            )
            connection.execute(
                """
                INSERT INTO runtime_artifacts(
                    artifact_id, graph_id, producer_node_id, artifact_type,
                    payload_ciphertext, source_versions_ciphertext,
                    verification_state, sensitivity, expires_at_ms, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact_id,
                    node["graph_id"],
                    node_id,
                    artifact.artifact_type,
                    payload,
                    versions,
                    artifact.verification_state.value,
                    artifact.sensitivity,
                    artifact.expires_at_ms,
                    now_ms,
                ),
            )
            connection.execute(
                """
                UPDATE runtime_agent_nodes
                SET status = 'completed', lease_owner = NULL, lease_expires_at_ms = NULL,
                    updated_at_ms = ? WHERE node_id = ?
                """,
                (now_ms, node_id),
            )
            connection.execute(
                "DELETE FROM runtime_resource_locks WHERE owner_node_id = ?", (node_id,)
            )
            self._release_parent_budget(connection, node, now_ms=now_ms)
            self._wake_parent(connection, node["parent_node_id"], now_ms=now_ms)
            return Artifact(
                artifact_id,
                node["graph_id"],
                node_id,
                artifact.artifact_type,
                artifact.payload,
                artifact.source_versions,
                artifact.verification_state,
            )

        return await self._database.transaction(operation)

    async def cancel_subtree(self, node_id: str, *, now_ms: int) -> list[str]:
        def operation(connection: sqlite3.Connection) -> list[str]:
            rows = connection.execute(
                """
                WITH RECURSIVE descendants(node_id) AS (
                    SELECT node_id FROM runtime_agent_nodes WHERE node_id = ?
                    UNION ALL
                    SELECT n.node_id FROM runtime_agent_nodes n
                    JOIN descendants d ON n.parent_node_id = d.node_id
                )
                SELECT node_id FROM descendants
                """,
                (node_id,),
            ).fetchall()
            ids = [row["node_id"] for row in rows]
            for current in ids:
                row = connection.execute(
                    "SELECT * FROM runtime_agent_nodes WHERE node_id = ?", (current,)
                ).fetchone()
                if row is None or row["status"] in self._terminal_node_values():
                    continue
                connection.execute(
                    """
                    UPDATE runtime_agent_nodes
                    SET status = 'cancelled', lease_owner = NULL, lease_expires_at_ms = NULL,
                        updated_at_ms = ? WHERE node_id = ?
                    """,
                    (now_ms, current),
                )
                connection.execute(
                    "DELETE FROM runtime_resource_locks WHERE owner_node_id = ?", (current,)
                )
                self._release_parent_budget(connection, row, now_ms=now_ms)
                self._block_dependents(connection, current, now_ms=now_ms)
            parents = {
                row["parent_node_id"]
                for current in ids
                if (
                    row := connection.execute(
                        "SELECT parent_node_id FROM runtime_agent_nodes WHERE node_id = ?",
                        (current,),
                    ).fetchone()
                )
                is not None
                and row["parent_node_id"] is not None
                and row["parent_node_id"] not in ids
            }
            for parent_id in parents:
                self._wake_parent(connection, parent_id, now_ms=now_ms)
            root = connection.execute(
                "SELECT graph_id FROM runtime_graphs WHERE root_node_id = ?", (node_id,)
            ).fetchone()
            if root is not None:
                connection.execute(
                    """
                    UPDATE runtime_graphs SET status = 'cancelled', updated_at_ms = ?
                    WHERE graph_id = ? AND status = 'active'
                    """,
                    (now_ms, root["graph_id"]),
                )
            return ids

        return await self._database.transaction(operation)

    async def send_mail(
        self,
        *,
        graph_id: str,
        sender_node_id: str,
        recipient_node_id: str,
        kind: MailboxKind,
        payload: dict[str, object],
        correlation_id: str | None,
        now_ms: int,
    ) -> MailboxItem:
        item_id = str(uuid4())

        def operation(connection: sqlite3.Connection) -> MailboxItem:
            nodes = connection.execute(
                """
                SELECT node_id FROM runtime_agent_nodes
                WHERE graph_id = ? AND node_id IN (?, ?)
                """,
                (graph_id, sender_node_id, recipient_node_id),
            ).fetchall()
            if {row["node_id"] for row in nodes} != {sender_node_id, recipient_node_id}:
                raise ValueError("mail sender and recipient must belong to graph")
            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1 FROM runtime_mailbox
                    WHERE recipient_node_id = ?
                    """,
                    (recipient_node_id,),
                ).fetchone()[0]
            )
            encrypted = self._encrypt_json(payload, aad=self._mail_aad(item_id))
            connection.execute(
                """
                INSERT INTO runtime_mailbox(
                    item_id, graph_id, sender_node_id, recipient_node_id, kind,
                    correlation_id, sequence, payload_ciphertext, state, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    item_id,
                    graph_id,
                    sender_node_id,
                    recipient_node_id,
                    kind.value,
                    correlation_id,
                    sequence,
                    encrypted,
                    now_ms,
                ),
            )
            return MailboxItem(
                item_id,
                graph_id,
                sender_node_id,
                recipient_node_id,
                kind,
                sequence,
                payload,
                MailboxState.PENDING,
                correlation_id,
            )

        return await self._database.transaction(operation)

    async def receive_mail(self, node_id: str, *, now_ms: int) -> list[MailboxItem]:
        def operation(connection: sqlite3.Connection) -> list[MailboxItem]:
            rows = connection.execute(
                """
                SELECT * FROM runtime_mailbox
                WHERE recipient_node_id = ? AND state IN ('pending', 'delivered')
                ORDER BY sequence
                """,
                (node_id,),
            ).fetchall()
            connection.execute(
                """
                UPDATE runtime_mailbox SET state = 'delivered',
                    delivered_at_ms = COALESCE(delivered_at_ms, ?)
                WHERE recipient_node_id = ? AND state = 'pending'
                """,
                (now_ms, node_id),
            )
            return [self._mail(row, MailboxState.DELIVERED) for row in rows]

        return await self._database.transaction(operation)

    async def acknowledge_mail(self, item_id: str, node_id: str, *, now_ms: int) -> None:
        def operation(connection: sqlite3.Connection) -> None:
            cursor = connection.execute(
                """
                UPDATE runtime_mailbox SET state = 'acknowledged', acknowledged_at_ms = ?
                WHERE item_id = ? AND recipient_node_id = ?
                  AND state IN ('pending', 'delivered', 'acknowledged')
                """,
                (now_ms, item_id, node_id),
            )
            if cursor.rowcount != 1:
                raise LookupError("mailbox item does not belong to recipient")

        await self._database.transaction(operation)

    async def acquire_lock(
        self,
        graph_id: str,
        node_id: str,
        target: ResourceTarget,
        *,
        now_ms: int,
        lease_ms: int,
    ) -> bool:
        if lease_ms < 1:
            raise ValueError("resource lock lease must be positive")

        def operation(connection: sqlite3.Connection) -> bool:
            node = connection.execute(
                """
                SELECT 1 FROM runtime_agent_nodes
                WHERE graph_id = ? AND node_id = ? AND status = 'running'
                """,
                (graph_id, node_id),
            ).fetchone()
            if node is None:
                raise ValueError("lock owner must be a running node in the graph")
            connection.execute(
                "DELETE FROM runtime_resource_locks WHERE lease_expires_at_ms <= ?", (now_ms,)
            )
            try:
                connection.execute(
                    """
                    INSERT INTO runtime_resource_locks(
                        tenant_key, resource_type, resource_id, graph_id,
                        owner_node_id, expected_revision, lease_expires_at_ms, created_at_ms
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        target.tenant_key,
                        target.resource_type,
                        target.resource_id,
                        graph_id,
                        node_id,
                        target.expected_revision,
                        now_ms + lease_ms,
                        now_ms,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = connection.execute(
                    """
                    SELECT owner_node_id FROM runtime_resource_locks
                    WHERE tenant_key = ? AND resource_type = ? AND resource_id = ?
                    """,
                    (target.tenant_key, target.resource_type, target.resource_id),
                ).fetchone()
                if existing is None or existing["owner_node_id"] != node_id:
                    return False
                connection.execute(
                    """
                    UPDATE runtime_resource_locks
                    SET lease_expires_at_ms = ?, expected_revision = ?
                    WHERE tenant_key = ? AND resource_type = ? AND resource_id = ?
                    """,
                    (
                        now_ms + lease_ms,
                        target.expected_revision,
                        target.tenant_key,
                        target.resource_type,
                        target.resource_id,
                    ),
                )
                return True
            return True

        return await self._database.transaction(operation)

    async def release_locks(self, node_id: str) -> None:
        await self._database.transaction(
            lambda connection: connection.execute(
                "DELETE FROM runtime_resource_locks WHERE owner_node_id = ?", (node_id,)
            )
        )

    async def list_artifacts(self, graph_id: str) -> list[Artifact]:
        rows = await self._database.call(
            lambda connection: connection.execute(
                "SELECT * FROM runtime_artifacts WHERE graph_id = ? ORDER BY created_at_ms",
                (graph_id,),
            ).fetchall()
        )
        return [self._artifact(row) for row in rows]

    async def save_checkpoint(
        self, node_id: str, state: dict[str, object], *, now_ms: int
    ) -> AgentCheckpoint:
        checkpoint_id = str(uuid4())

        def operation(connection: sqlite3.Connection) -> AgentCheckpoint:
            node = connection.execute(
                "SELECT graph_id FROM runtime_agent_nodes WHERE node_id = ?", (node_id,)
            ).fetchone()
            if node is None:
                raise LookupError("Agent node does not exist")
            sequence = int(
                connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1
                    FROM runtime_agent_checkpoints WHERE node_id = ?
                    """,
                    (node_id,),
                ).fetchone()[0]
            )
            encrypted = self._encrypt_json(state, aad=self._checkpoint_aad(checkpoint_id))
            connection.execute(
                """
                INSERT INTO runtime_agent_checkpoints(
                    checkpoint_id, graph_id, node_id, sequence,
                    state_ciphertext, created_at_ms
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    checkpoint_id,
                    node["graph_id"],
                    node_id,
                    sequence,
                    encrypted,
                    now_ms,
                ),
            )
            return AgentCheckpoint(
                checkpoint_id,
                node["graph_id"],
                node_id,
                sequence,
                state,
                now_ms,
            )

        return await self._database.transaction(operation)

    async def latest_checkpoint(self, node_id: str) -> AgentCheckpoint | None:
        row = await self._database.call(
            lambda connection: connection.execute(
                """
                SELECT * FROM runtime_agent_checkpoints
                WHERE node_id = ? ORDER BY sequence DESC LIMIT 1
                """,
                (node_id,),
            ).fetchone()
        )
        if row is None:
            return None
        return AgentCheckpoint(
            row["checkpoint_id"],
            row["graph_id"],
            row["node_id"],
            row["sequence"],
            self._decrypt_json(
                row["state_ciphertext"],
                aad=self._checkpoint_aad(row["checkpoint_id"]),
            ),
            row["created_at_ms"],
        )

    async def finish_graph(
        self, graph_id: str, node_id: str, status: GraphStatus, *, now_ms: int
    ) -> None:
        if status is GraphStatus.ACTIVE:
            raise ValueError("finish_graph requires a terminal status")

        def operation(connection: sqlite3.Connection) -> None:
            graph = connection.execute(
                "SELECT root_node_id, status FROM runtime_graphs WHERE graph_id = ?", (graph_id,)
            ).fetchone()
            if graph is None:
                raise LookupError("graph does not exist")
            if graph["root_node_id"] != node_id:
                raise PermissionError("only the root Agent may publish the terminal result")
            if graph["status"] != GraphStatus.ACTIVE.value:
                raise RuntimeError("graph is already terminal")
            connection.execute(
                "UPDATE runtime_graphs SET status = ?, updated_at_ms = ? WHERE graph_id = ?",
                (status.value, now_ms, graph_id),
            )

        await self._database.transaction(operation)

    def _insert_node(
        self,
        connection: sqlite3.Connection,
        *,
        node_id: str,
        graph_id: str,
        parent_node_id: str | None,
        canonical_path: str,
        spec: NodeSpec,
        depth: int,
        now_ms: int,
    ) -> None:
        task = self._cipher.encrypt(
            spec.task_brief.encode(), associated_data=self._node_aad(node_id, "task")
        )
        tools = self._encrypt_json(
            {"tool_ids": list(spec.tool_ids)}, aad=self._node_aad(node_id, "tools")
        )
        budget = self._encrypt_json(spec.budgets, aad=self._node_aad(node_id, "budget"))
        connection.execute(
            """
            INSERT INTO runtime_agent_nodes(
                node_id, graph_id, parent_node_id, canonical_path, name, role,
                task_brief_ciphertext, expected_output_type, context_mode,
                tool_ids_ciphertext, budget_ciphertext, deadline_ms, depth, status,
                attempt_count, created_at_ms, updated_at_ms
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', 0, ?, ?)
            """,
            (
                node_id,
                graph_id,
                parent_node_id,
                canonical_path,
                spec.name,
                spec.role.value,
                task,
                spec.expected_output_type,
                spec.context_mode.value,
                tools,
                budget,
                spec.deadline_ms,
                depth,
                now_ms,
                now_ms,
            ),
        )
        for kind, maximum in spec.budgets.items():
            connection.execute(
                """
                INSERT INTO runtime_budget_ledger(
                    graph_id, node_id, budget_kind, maximum, reserved, consumed, updated_at_ms
                ) VALUES (?, ?, ?, ?, 0, 0, ?)
                """,
                (graph_id, node_id, kind, maximum, now_ms),
            )

    def _reserve_parent_budget(
        self,
        connection: sqlite3.Connection,
        graph_id: str,
        parent_node_id: str,
        budgets: dict[str, int],
        *,
        now_ms: int,
    ) -> None:
        for kind, amount in budgets.items():
            cursor = connection.execute(
                """
                UPDATE runtime_budget_ledger
                SET reserved = reserved + ?, updated_at_ms = ?
                WHERE graph_id = ? AND node_id = ? AND budget_kind = ?
                  AND maximum - reserved - consumed >= ?
                """,
                (amount, now_ms, graph_id, parent_node_id, kind, amount),
            )
            if cursor.rowcount != 1:
                raise ValueError(f"insufficient parent budget: {kind}")

    def _release_parent_budget(
        self, connection: sqlite3.Connection, node: sqlite3.Row, *, now_ms: int
    ) -> None:
        parent_id = node["parent_node_id"]
        if parent_id is None:
            return
        budgets = self._decrypt_json(
            node["budget_ciphertext"], aad=self._node_aad(node["node_id"], "budget")
        )
        for kind, amount in budgets.items():
            connection.execute(
                """
                UPDATE runtime_budget_ledger
                SET reserved = MAX(0, reserved - ?), updated_at_ms = ?
                WHERE graph_id = ? AND node_id = ? AND budget_kind = ?
                """,
                (int(amount), now_ms, node["graph_id"], parent_id, kind),
            )

    @staticmethod
    def _wake_parent(
        connection: sqlite3.Connection, parent_node_id: str | None, *, now_ms: int
    ) -> None:
        if parent_node_id is None:
            return
        open_child = connection.execute(
            """
            SELECT 1 FROM runtime_agent_nodes
            WHERE parent_node_id = ?
              AND status NOT IN ('completed', 'blocked', 'failed', 'cancelled')
            LIMIT 1
            """,
            (parent_node_id,),
        ).fetchone()
        if open_child is None:
            connection.execute(
                """
                UPDATE runtime_agent_nodes SET status = 'ready', updated_at_ms = ?
                WHERE node_id = ? AND status = 'waiting'
                """,
                (now_ms, parent_node_id),
            )

    def _block_dependents(
        self, connection: sqlite3.Connection, predecessor_node_id: str, *, now_ms: int
    ) -> None:
        rows = connection.execute(
            """
            SELECT n.* FROM runtime_agent_edges e
            JOIN runtime_agent_nodes n ON n.node_id = e.dependent_node_id
            WHERE e.predecessor_node_id = ? AND e.edge_kind = 'depends_on'
              AND n.status NOT IN ('completed', 'blocked', 'failed', 'cancelled')
            """,
            (predecessor_node_id,),
        ).fetchall()
        for node in rows:
            connection.execute(
                """
                UPDATE runtime_agent_nodes
                SET status = 'blocked', lease_owner = NULL, lease_expires_at_ms = NULL,
                    updated_at_ms = ? WHERE node_id = ?
                """,
                (now_ms, node["node_id"]),
            )
            self._release_parent_budget(connection, node, now_ms=now_ms)
            self._wake_parent(connection, node["parent_node_id"], now_ms=now_ms)

    def _graph(self, row: sqlite3.Row) -> GraphRecord:
        return GraphRecord(
            graph_id=row["graph_id"],
            root_run_id=row["root_run_id"],
            root_node_id=row["root_node_id"],
            tenant_key=row["tenant_key"],
            app_id=row["app_id"],
            source_resource_kind=row["source_resource_kind"],
            source_resource_id=row["source_resource_id"],
            agent_definition_id=row["agent_definition_id"],
            agent_definition_version=row["agent_definition_version"],
            status=GraphStatus(row["status"]),
            limits=GraphLimits(row["max_depth"], row["max_nodes"], row["max_concurrency"]),
        )

    def _node(self, row: sqlite3.Row, dependency_node_ids: tuple[str, ...] = ()) -> AgentNode:
        tools = self._decrypt_json(
            row["tool_ids_ciphertext"], aad=self._node_aad(row["node_id"], "tools")
        )
        budgets = self._decrypt_json(
            row["budget_ciphertext"], aad=self._node_aad(row["node_id"], "budget")
        )
        spec = NodeSpec(
            name=row["name"],
            role=AgentRole(row["role"]),
            task_brief=self._cipher.decrypt(
                row["task_brief_ciphertext"],
                associated_data=self._node_aad(row["node_id"], "task"),
            ).decode(),
            expected_output_type=row["expected_output_type"],
            tool_ids=tuple(str(item) for item in tools["tool_ids"]),
            budgets={str(key): int(value) for key, value in budgets.items()},
            context_mode=ContextMode(row["context_mode"]),
            deadline_ms=row["deadline_ms"],
            dependency_node_ids=dependency_node_ids,
        )
        return AgentNode(
            node_id=row["node_id"],
            graph_id=row["graph_id"],
            parent_node_id=row["parent_node_id"],
            canonical_path=row["canonical_path"],
            spec=spec,
            depth=row["depth"],
            status=NodeStatus(row["status"]),
            attempt_count=row["attempt_count"],
            lease_owner=row["lease_owner"],
            lease_expires_at_ms=row["lease_expires_at_ms"],
        )

    def _node_with_lease(
        self,
        row: sqlite3.Row,
        *,
        dependency_node_ids: tuple[str, ...],
        worker_id: str,
        lease_expires_at_ms: int,
    ) -> AgentNode:
        node = self._node(row, dependency_node_ids)
        return AgentNode(
            node_id=node.node_id,
            graph_id=node.graph_id,
            parent_node_id=node.parent_node_id,
            canonical_path=node.canonical_path,
            spec=node.spec,
            depth=node.depth,
            status=NodeStatus.RUNNING,
            attempt_count=node.attempt_count + 1,
            lease_owner=worker_id,
            lease_expires_at_ms=lease_expires_at_ms,
        )

    def _mail(self, row: sqlite3.Row, state: MailboxState) -> MailboxItem:
        return MailboxItem(
            item_id=row["item_id"],
            graph_id=row["graph_id"],
            sender_node_id=row["sender_node_id"],
            recipient_node_id=row["recipient_node_id"],
            kind=MailboxKind(row["kind"]),
            sequence=row["sequence"],
            payload=self._decrypt_json(
                row["payload_ciphertext"], aad=self._mail_aad(row["item_id"])
            ),
            state=state,
            correlation_id=row["correlation_id"],
        )

    def _artifact(self, row: sqlite3.Row) -> Artifact:
        return Artifact(
            artifact_id=row["artifact_id"],
            graph_id=row["graph_id"],
            producer_node_id=row["producer_node_id"],
            artifact_type=row["artifact_type"],
            payload=self._decrypt_json(
                row["payload_ciphertext"], aad=self._artifact_aad(row["artifact_id"], "payload")
            ),
            source_versions={
                str(key): str(value)
                for key, value in self._decrypt_json(
                    row["source_versions_ciphertext"],
                    aad=self._artifact_aad(row["artifact_id"], "sources"),
                ).items()
            },
            verification_state=VerificationState(row["verification_state"]),
        )

    @staticmethod
    def _dependencies(connection: sqlite3.Connection, node_id: str) -> tuple[str, ...]:
        rows = connection.execute(
            """
            SELECT predecessor_node_id FROM runtime_agent_edges
            WHERE dependent_node_id = ? AND edge_kind = 'depends_on'
            ORDER BY predecessor_node_id
            """,
            (node_id,),
        ).fetchall()
        return tuple(row["predecessor_node_id"] for row in rows)

    def _encrypt_json(self, value: dict[str, Any], *, aad: bytes) -> bytes:
        return self._cipher.encrypt(
            json.dumps(value, ensure_ascii=False, sort_keys=True).encode(), associated_data=aad
        )

    def _decrypt_json(self, value: bytes, *, aad: bytes) -> dict[str, Any]:
        decoded = json.loads(self._cipher.decrypt(value, associated_data=aad))
        if not isinstance(decoded, dict):
            raise ValueError("stored Agent coordination payload must be an object")
        return decoded

    @staticmethod
    def _node_aad(node_id: str, field: str) -> bytes:
        return f"runtime_agent_nodes:{node_id}:{field}:v1".encode()

    @staticmethod
    def _mail_aad(item_id: str) -> bytes:
        return f"runtime_mailbox:{item_id}:v1".encode()

    @staticmethod
    def _artifact_aad(artifact_id: str, field: str) -> bytes:
        return f"runtime_artifacts:{artifact_id}:{field}:v1".encode()

    @staticmethod
    def _checkpoint_aad(checkpoint_id: str) -> bytes:
        return f"runtime_agent_checkpoints:{checkpoint_id}:state:v1".encode()

    @staticmethod
    def _terminal_node_values() -> tuple[str, ...]:
        return (
            NodeStatus.COMPLETED.value,
            NodeStatus.BLOCKED.value,
            NodeStatus.FAILED.value,
            NodeStatus.CANCELLED.value,
        )
