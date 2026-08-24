from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from codex2lark.runtime.multi_agent import (
    AgentRole,
    ArtifactDraft,
    ContextMode,
    GraphLimits,
    GraphStatus,
    MailboxKind,
    MultiAgentSupervisor,
    NodeExecutionInput,
    NodeExecutionResult,
    NodeSpec,
    NodeStatus,
    ResourceTarget,
)
from codex2lark.runtime.tools import WriteScopeTarget
from codex2lark.runtime.types import RunStatus, VerificationState
from codex2lark.storage.agent_store import SQLiteAgentGraphStore
from codex2lark.storage.crypto import EnvelopeCipher, MasterKey
from codex2lark.storage.database import SQLiteDatabase


class ConcurrentWorker:
    def __init__(self, expected: int) -> None:
        self.expected = expected
        self.active = 0
        self.maximum_active = 0
        self.all_started = asyncio.Event()

    async def execute(self, execution: NodeExecutionInput) -> NodeExecutionResult:
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        if self.active == self.expected:
            self.all_started.set()
        await asyncio.wait_for(self.all_started.wait(), timeout=1)
        self.active -= 1
        return NodeExecutionResult(
            ArtifactDraft(
                execution.node.spec.expected_output_type,
                {"claims": {execution.node.spec.name: "done"}},
                {},
                VerificationState.VERIFIED,
            )
        )


def root_spec() -> NodeSpec:
    return NodeSpec(
        name="root",
        role=AgentRole.ORCHESTRATOR,
        task_brief="Own the verified user outcome.",
        expected_output_type="AgentOutcome",
        tool_ids=("docs.create", "docs.read", "sheets.update"),
        budgets={"model_tokens": 100_000, "tool_calls": 20, "external_writes": 5},
    )


def child_spec(
    name: str,
    role: AgentRole,
    *,
    tools: tuple[str, ...] = ("docs.read",),
    dependency_ids: tuple[str, ...] = (),
) -> NodeSpec:
    return NodeSpec(
        name=name,
        role=role,
        task_brief=f"Complete the bounded {name} deliverable.",
        expected_output_type="ResearchBundle",
        tool_ids=tools,
        budgets={"model_tokens": 20_000, "tool_calls": 4, "external_writes": 0},
        context_mode=ContextMode.SELECTED,
        dependency_node_ids=dependency_ids,
    )


async def setup_graph(tmp_path: Path, *, limits: GraphLimits | None = None):
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    cipher = EnvelopeCipher(MasterKey("test", b"m" * 32))
    store = SQLiteAgentGraphStore(database, cipher)
    supervisor = MultiAgentSupervisor(store)
    graph, root = await supervisor.create_graph(
        root_run_id="run-1",
        tenant_key="tenant-1",
        app_id="app-1",
        source_resource_kind="im.thread",
        source_resource_id="thread-1",
        agent_definition_id="default",
        agent_definition_version=1,
        root_spec=root_spec(),
        limits=limits or GraphLimits(),
        now_ms=1,
    )
    return database, store, supervisor, graph, root


async def test_spawn_enforces_roles_tools_depth_nodes_and_budget(tmp_path: Path) -> None:
    database, store, supervisor, graph, root = await setup_graph(
        tmp_path, limits=GraphLimits(max_depth=2, max_nodes=4, max_concurrency=2)
    )
    try:
        researcher = await supervisor.spawn(
            graph.graph_id,
            root.node_id,
            child_spec("research", AgentRole.RESEARCHER),
            now_ms=2,
        )
        assert researcher.canonical_path == "/root/research"

        with pytest.raises(PermissionError, match="tool authority"):
            await supervisor.spawn(
                graph.graph_id,
                root.node_id,
                child_spec("unsafe", AgentRole.AUTHOR, tools=("drive.delete",)),
                now_ms=3,
            )
        with pytest.raises(PermissionError, match="cannot delegate"):
            await supervisor.spawn(
                graph.graph_id,
                researcher.node_id,
                child_spec("nested", AgentRole.VERIFIER),
                now_ms=3,
            )

        author = await supervisor.spawn(
            graph.graph_id,
            root.node_id,
            child_spec("author", AgentRole.AUTHOR, tools=("docs.create",)),
            now_ms=4,
        )
        with pytest.raises(ValueError, match="depth limit"):
            await supervisor.spawn(
                graph.graph_id,
                author.node_id,
                child_spec("verify", AgentRole.VERIFIER, tools=()),
                now_ms=5,
            )
        await supervisor.spawn(
            graph.graph_id,
            root.node_id,
            child_spec("extra", AgentRole.RESEARCHER),
            now_ms=5,
        )
        with pytest.raises(ValueError, match="node limit"):
            await supervisor.spawn(
                graph.graph_id,
                root.node_id,
                child_spec("overflow", AgentRole.RESEARCHER),
                now_ms=6,
            )
        assert len(await store.list_nodes(graph.graph_id)) == 4
    finally:
        await database.close()


async def test_dependencies_concurrency_leases_and_restart_recovery(tmp_path: Path) -> None:
    database, store, supervisor, graph, root = await setup_graph(tmp_path)
    try:
        first = await supervisor.spawn(
            graph.graph_id,
            root.node_id,
            child_spec("first", AgentRole.RESEARCHER),
            now_ms=2,
        )
        second = await supervisor.spawn(
            graph.graph_id,
            root.node_id,
            child_spec("second", AgentRole.RESEARCHER, dependency_ids=(first.node_id,)),
            now_ms=3,
        )

        leased = await store.lease_ready(
            graph.graph_id, worker_id="worker-a", now_ms=10, lease_ms=10, limit=3
        )
        assert [node.node_id for node in leased] == [first.node_id]
        recovered = await store.lease_ready(
            graph.graph_id, worker_id="worker-b", now_ms=21, lease_ms=10, limit=3
        )
        assert recovered[0].node_id == first.node_id
        assert recovered[0].attempt_count == 2

        await store.complete_node(
            first.node_id,
            worker_id="worker-b",
            artifact=ArtifactDraft(
                "ResearchBundle",
                {"claims": {"answer": 42}},
                {"message-1": "v1"},
                VerificationState.VERIFIED,
            ),
            now_ms=22,
        )
        unblocked = await store.lease_ready(
            graph.graph_id, worker_id="worker-c", now_ms=23, lease_ms=10, limit=3
        )
        assert unblocked[0].node_id == second.node_id
        assert unblocked[0].spec.dependency_node_ids == (first.node_id,)
    finally:
        await database.close()


async def test_mailbox_is_durable_redelivered_and_acknowledged(tmp_path: Path) -> None:
    database, store, supervisor, graph, root = await setup_graph(tmp_path)
    try:
        child = await supervisor.spawn(
            graph.graph_id,
            root.node_id,
            child_spec("research", AgentRole.RESEARCHER),
            now_ms=2,
        )
        waiting = asyncio.create_task(
            supervisor.wait_for_mail(child.node_id, now_ms=3, timeout_s=1)
        )
        await asyncio.sleep(0)
        sent = await supervisor.send(
            graph_id=graph.graph_id,
            sender_node_id=root.node_id,
            recipient_node_id=child.node_id,
            kind=MailboxKind.STEER,
            payload={"instruction": "Focus on primary sources."},
            now_ms=4,
        )
        received = await waiting
        assert received[0].item_id == sent.item_id
        assert received[0].payload["instruction"] == "Focus on primary sources."

        redelivered = await store.receive_mail(child.node_id, now_ms=5)
        assert redelivered[0].item_id == sent.item_id
        await store.acknowledge_mail(sent.item_id, child.node_id, now_ms=6)
        await store.acknowledge_mail(sent.item_id, child.node_id, now_ms=7)
        assert await store.receive_mail(child.node_id, now_ms=8) == []

        first = await supervisor.send(
            graph_id=graph.graph_id,
            sender_node_id=root.node_id,
            recipient_node_id=child.node_id,
            kind=MailboxKind.MESSAGE,
            payload={"text": "stable update"},
            correlation_id="stable-correlation",
            now_ms=9,
        )
        duplicate = await supervisor.send(
            graph_id=graph.graph_id,
            sender_node_id=root.node_id,
            recipient_node_id=child.node_id,
            kind=MailboxKind.MESSAGE,
            payload={"text": "stable update"},
            correlation_id="stable-correlation",
            now_ms=10,
        )
        assert duplicate.item_id == first.item_id
        with pytest.raises(RuntimeError, match="identity collision"):
            await supervisor.send(
                graph_id=graph.graph_id,
                sender_node_id=root.node_id,
                recipient_node_id=child.node_id,
                kind=MailboxKind.MESSAGE,
                payload={"text": "different update"},
                correlation_id="stable-correlation",
                now_ms=11,
            )
    finally:
        await database.close()


async def test_resource_lock_excludes_overlapping_writers_and_recovers(tmp_path: Path) -> None:
    database, store, supervisor, graph, root = await setup_graph(tmp_path)
    try:
        first = await supervisor.spawn(
            graph.graph_id,
            root.node_id,
            child_spec("writer_one", AgentRole.AUTHOR, tools=("docs.create",)),
            now_ms=2,
        )
        second = await supervisor.spawn(
            graph.graph_id,
            root.node_id,
            child_spec("writer_two", AgentRole.AUTHOR, tools=("docs.create",)),
            now_ms=3,
        )
        target = ResourceTarget("tenant-1", "docx", "docx-1", "revision-1")
        leased = await store.lease_ready(
            graph.graph_id, worker_id="worker", now_ms=9, lease_ms=20, limit=3
        )
        assert {node.node_id for node in leased} == {first.node_id, second.node_id}

        assert await store.acquire_lock(
            graph.graph_id, first.node_id, target, now_ms=10, lease_ms=10
        )
        assert not await store.acquire_lock(
            graph.graph_id, second.node_id, target, now_ms=11, lease_ms=10
        )
        assert await store.acquire_lock(
            graph.graph_id, second.node_id, target, now_ms=21, lease_ms=10
        )
    finally:
        await database.close()


async def test_created_writer_is_not_leased_until_lock_and_activation(tmp_path: Path) -> None:
    database, store, supervisor, graph, root = await setup_graph(tmp_path)
    try:
        child = await supervisor.spawn(
            graph.graph_id,
            root.node_id,
            replace(
                child_spec("held_writer", AgentRole.AUTHOR, tools=("docs.create",)),
                requires_write_scope=True,
            ),
            ready=False,
            now_ms=2,
        )
        assert child.status is NodeStatus.CREATED
        assert (
            await store.lease_ready(
                graph.graph_id, worker_id="worker", now_ms=3, lease_ms=20, limit=3
            )
            == []
        )

        target = ResourceTarget("tenant-1", "docx", "docx-held", "revision-1")
        assert await store.acquire_lock(
            graph.graph_id, child.node_id, target, now_ms=4, lease_ms=20
        )
        activated = await supervisor.activate(child.node_id, now_ms=5)
        leased = await store.lease_ready(
            graph.graph_id, worker_id="worker", now_ms=6, lease_ms=20, limit=3
        )

        assert activated.status is NodeStatus.READY
        assert activated.spec.requires_write_scope is True
        assert [item.node_id for item in leased] == [child.node_id]
        assert await store.list_locks(child.node_id, now_ms=6) == (target,)
        assert await store.owns_write_scope(
            child.node_id,
            (WriteScopeTarget("docx", "docx-held", "revision-1"),),
            now_ms=23,
        )
        assert not await store.owns_write_scope(
            child.node_id,
            (WriteScopeTarget("docx", "docx-held", "revision-1"),),
            now_ms=24,
        )
    finally:
        await database.close()


async def test_cancellation_cascades_and_root_only_finishes_graph(tmp_path: Path) -> None:
    database, store, supervisor, graph, root = await setup_graph(tmp_path)
    try:
        author = await supervisor.spawn(
            graph.graph_id,
            root.node_id,
            child_spec("author", AgentRole.AUTHOR, tools=("docs.create",)),
            now_ms=2,
        )
        verifier = await supervisor.spawn(
            graph.graph_id,
            author.node_id,
            child_spec("verify", AgentRole.VERIFIER, tools=()),
            now_ms=3,
        )
        cancelled = await supervisor.cancel(author.node_id, now_ms=4)
        assert set(cancelled) == {author.node_id, verifier.node_id}
        statuses = {node.node_id: node.status for node in await store.list_nodes(graph.graph_id)}
        assert statuses[author.node_id] is NodeStatus.CANCELLED
        assert statuses[verifier.node_id] is NodeStatus.CANCELLED

        with pytest.raises(PermissionError, match="root Agent"):
            await supervisor.publish_terminal(
                graph.graph_id, author.node_id, RunStatus.FAILED, now_ms=5
            )
        await supervisor.publish_terminal(graph.graph_id, root.node_id, RunStatus.BLOCKED, now_ms=6)
        assert (await store.get_graph(graph.graph_id)).status is GraphStatus.BLOCKED
    finally:
        await database.close()


async def test_artifacts_commit_with_completion_merge_conflicts_and_stay_encrypted(
    tmp_path: Path,
) -> None:
    database, store, supervisor, graph, root = await setup_graph(tmp_path)
    try:
        first = await supervisor.spawn(
            graph.graph_id,
            root.node_id,
            child_spec("research_one", AgentRole.RESEARCHER),
            now_ms=2,
        )
        second = await supervisor.spawn(
            graph.graph_id,
            root.node_id,
            child_spec("research_two", AgentRole.RESEARCHER),
            now_ms=3,
        )
        leased = await store.lease_ready(
            graph.graph_id, worker_id="worker", now_ms=4, lease_ms=100, limit=3
        )
        assert {node.node_id for node in leased} == {first.node_id, second.node_id}
        for node, answer in ((first, 41), (second, 42)):
            await store.complete_node(
                node.node_id,
                worker_id="worker",
                artifact=ArtifactDraft(
                    "ResearchBundle",
                    {"claims": {"answer": answer}, "private": "group content"},
                    {"message-1": "v1"},
                    VerificationState.VERIFIED,
                ),
                now_ms=5,
            )
        artifacts = await store.list_artifacts(graph.graph_id)
        merged = supervisor.merge_artifacts(artifacts)
        assert merged.conflicts == {"answer": (41, 42)}
        ciphertext = await database.call(
            lambda connection: connection.execute(
                "SELECT payload_ciphertext FROM runtime_artifacts LIMIT 1"
            ).fetchone()[0]
        )
        assert b"group content" not in ciphertext
    finally:
        await database.close()


async def test_agent_checkpoint_is_monotonic_encrypted_and_restart_readable(
    tmp_path: Path,
) -> None:
    database, store, _supervisor, _graph, root = await setup_graph(tmp_path)
    try:
        first = await store.save_checkpoint(
            root.node_id, {"turn": 1, "summary": "private group state"}, now_ms=2
        )
        second = await store.save_checkpoint(
            root.node_id, {"turn": 2, "summary": "safe complete turn"}, now_ms=3
        )
        assert first.sequence == 1
        assert second.sequence == 2
        ciphertext = await database.call(
            lambda connection: connection.execute(
                """
                SELECT state_ciphertext FROM runtime_agent_checkpoints
                WHERE checkpoint_id = ?
                """,
                (second.checkpoint_id,),
            ).fetchone()[0]
        )
        assert b"safe complete turn" not in ciphertext
        assert await store.latest_checkpoint(root.node_id) == second
    finally:
        await database.close()


async def test_supervisor_executes_three_independent_workers_concurrently(
    tmp_path: Path,
) -> None:
    database, store, supervisor, graph, root = await setup_graph(tmp_path)
    try:
        for name, role in (
            ("research", AgentRole.RESEARCHER),
            ("author", AgentRole.AUTHOR),
            ("analysis", AgentRole.DATA_ANALYST),
        ):
            await supervisor.spawn(
                graph.graph_id,
                root.node_id,
                child_spec(name, role),
                now_ms=2,
            )
        worker = ConcurrentWorker(expected=3)

        batch = await supervisor.execute_ready(
            graph.graph_id,
            worker_id="pool-1",
            worker=worker,
            now_ms=3,
            lease_ms=100,
        )

        assert len(batch.completed_artifacts) == 3
        assert batch.failed_node_ids == ()
        assert worker.maximum_active == 3
        root_after = await store.get_node(root.node_id)
        assert root_after.status is NodeStatus.READY
    finally:
        await database.close()


async def test_three_worker_graph_recovers_mail_checkpoint_and_lock_after_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "runtime.db"
    key = MasterKey("test", b"r" * 32)
    first_database = SQLiteDatabase(path)
    await first_database.open()
    first_store = SQLiteAgentGraphStore(first_database, EnvelopeCipher(key))
    first_supervisor = MultiAgentSupervisor(first_store)
    graph, root = await first_supervisor.create_graph(
        root_run_id="restart-run",
        tenant_key="tenant-1",
        app_id="app-1",
        source_resource_kind="im.thread",
        source_resource_id="thread-restart",
        agent_definition_id="default",
        agent_definition_version=1,
        root_spec=root_spec(),
        limits=GraphLimits(max_concurrency=3),
        now_ms=1,
    )
    children = [
        await first_supervisor.spawn(
            graph.graph_id,
            root.node_id,
            child_spec(name, role),
            now_ms=2,
        )
        for name, role in (
            ("research", AgentRole.RESEARCHER),
            ("author", AgentRole.AUTHOR),
            ("analysis", AgentRole.DATA_ANALYST),
        )
    ]
    checkpoint = await first_store.save_checkpoint(
        root.node_id, {"turn": 2, "state": "complete safe turn"}, now_ms=2
    )
    mailbox = await first_supervisor.send(
        graph_id=graph.graph_id,
        sender_node_id=root.node_id,
        recipient_node_id=children[0].node_id,
        kind=MailboxKind.STEER,
        payload={"instruction": "Use the newest authorized evidence."},
        now_ms=2,
    )
    crashed = await first_store.lease_ready(
        graph.graph_id,
        worker_id="crashed-process",
        now_ms=3,
        lease_ms=100,
        limit=3,
    )
    assert len(crashed) == 3
    assert await first_store.acquire_lock(
        graph.graph_id,
        children[1].node_id,
        ResourceTarget("tenant-1", "docx", "docx-restart", "revision-1"),
        now_ms=4,
        lease_ms=99,
    )
    await first_database.close()

    recovered_database = SQLiteDatabase(path)
    await recovered_database.open()
    recovered_store = SQLiteAgentGraphStore(recovered_database, EnvelopeCipher(key))
    recovered_supervisor = MultiAgentSupervisor(recovered_store)
    try:
        assert (
            await recovered_store.lease_ready(
                graph.graph_id,
                worker_id="early-process",
                now_ms=102,
                lease_ms=100,
                limit=3,
            )
            == []
        )
        assert await recovered_store.latest_checkpoint(root.node_id) == checkpoint
        redelivered = await recovered_store.receive_mail(children[0].node_id, now_ms=103)
        assert [item.item_id for item in redelivered] == [mailbox.item_id]
        await recovered_store.acknowledge_mail(mailbox.item_id, children[0].node_id, now_ms=103)

        worker = ConcurrentWorker(expected=3)
        batch = await recovered_supervisor.execute_ready(
            graph.graph_id,
            worker_id="recovery-process",
            worker=worker,
            now_ms=104,
            lease_ms=100,
        )

        assert len(batch.completed_artifacts) == 3
        assert batch.failed_node_ids == ()
        assert worker.maximum_active == 3
        assert len(await recovered_store.list_artifacts(graph.graph_id)) == 3
        assert await recovered_store.list_locks(children[1].node_id, now_ms=104) == ()
        recovered_nodes = {
            node.node_id: node for node in await recovered_store.list_nodes(graph.graph_id)
        }
        assert all(recovered_nodes[item.node_id].attempt_count == 2 for item in children)
        assert recovered_nodes[root.node_id].status is NodeStatus.READY

        await recovered_supervisor.publish_terminal(
            graph.graph_id, root.node_id, RunStatus.COMPLETED, now_ms=105
        )
        assert (await recovered_store.get_graph(graph.graph_id)).status is GraphStatus.COMPLETED
    finally:
        await recovered_database.close()
