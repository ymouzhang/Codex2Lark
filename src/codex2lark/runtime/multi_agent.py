from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Protocol
from uuid import uuid4

from .tools import WriteScopeTarget
from .types import RunStatus, VerificationState


class AgentRole(StrEnum):
    ORCHESTRATOR = "orchestrator"
    RESEARCHER = "researcher"
    AUTHOR = "author"
    DATA_ANALYST = "data_analyst"
    VERIFIER = "verifier"
    OPERATOR = "operator"


class ContextMode(StrEnum):
    NONE = "none"
    SELECTED = "selected"
    FULL_SAFE_HISTORY = "full_safe_history"


class GraphStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"


class NodeStatus(StrEnum):
    CREATED = "created"
    READY = "ready"
    RUNNING = "running"
    WAITING = "waiting"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"


class MailboxKind(StrEnum):
    TASK = "task"
    MESSAGE = "message"
    FOLLOW_UP = "follow_up"
    STEER = "steer"
    ARTIFACT = "artifact"
    QUESTION = "question"
    ANSWER = "answer"
    CANCEL = "cancel"
    STATUS = "status"


class MailboxState(StrEnum):
    PENDING = "pending"
    DELIVERED = "delivered"
    ACKNOWLEDGED = "acknowledged"


@dataclass(frozen=True, slots=True)
class GraphLimits:
    max_depth: int = 3
    max_nodes: int = 8
    max_concurrency: int = 3

    def __post_init__(self) -> None:
        if min(self.max_depth, self.max_nodes, self.max_concurrency) < 1:
            raise ValueError("graph limits must be positive")


@dataclass(frozen=True, slots=True)
class NodeSpec:
    name: str
    role: AgentRole
    task_brief: str
    expected_output_type: str
    tool_ids: tuple[str, ...]
    budgets: dict[str, int]
    context_mode: ContextMode = ContextMode.SELECTED
    deadline_ms: int | None = None
    dependency_node_ids: tuple[str, ...] = ()
    requires_write_scope: bool = False

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9_]+", self.name):
            raise ValueError("node name must contain lowercase letters, digits, or underscores")
        if not self.task_brief or not self.expected_output_type:
            raise ValueError("node task brief and output type are required")
        if len(set(self.tool_ids)) != len(self.tool_ids):
            raise ValueError("node tool_ids must be unique")
        if any(value < 0 for value in self.budgets.values()):
            raise ValueError("node budgets cannot be negative")


@dataclass(frozen=True, slots=True)
class GraphRecord:
    graph_id: str
    root_run_id: str
    root_node_id: str
    tenant_key: str
    app_id: str
    source_resource_kind: str
    source_resource_id: str
    agent_definition_id: str
    agent_definition_version: int
    status: GraphStatus
    limits: GraphLimits


@dataclass(frozen=True, slots=True)
class AgentNode:
    node_id: str
    graph_id: str
    parent_node_id: str | None
    canonical_path: str
    spec: NodeSpec
    depth: int
    status: NodeStatus
    attempt_count: int = 0
    lease_owner: str | None = None
    lease_expires_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class MailboxItem:
    item_id: str
    graph_id: str
    sender_node_id: str
    recipient_node_id: str
    kind: MailboxKind
    sequence: int
    payload: dict[str, object]
    state: MailboxState
    correlation_id: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactDraft:
    artifact_type: str
    payload: dict[str, object]
    source_versions: dict[str, str]
    verification_state: VerificationState
    sensitivity: str = "business"
    expires_at_ms: int | None = None


@dataclass(frozen=True, slots=True)
class Artifact:
    artifact_id: str
    graph_id: str
    producer_node_id: str
    artifact_type: str
    payload: dict[str, object]
    source_versions: dict[str, str]
    verification_state: VerificationState


@dataclass(frozen=True, slots=True)
class AgentCheckpoint:
    checkpoint_id: str
    graph_id: str
    node_id: str
    sequence: int
    state: dict[str, object]
    created_at_ms: int


@dataclass(frozen=True, slots=True)
class ResourceTarget:
    tenant_key: str
    resource_type: str
    resource_id: str
    expected_revision: str | None = None


@dataclass(frozen=True, slots=True)
class ArtifactMerge:
    artifacts: tuple[Artifact, ...]
    claims: dict[str, object]
    conflicts: dict[str, tuple[object, ...]]


@dataclass(frozen=True, slots=True)
class NodeExecutionInput:
    node: AgentNode
    mailbox: tuple[MailboxItem, ...]
    dependency_artifacts: tuple[Artifact, ...]


@dataclass(frozen=True, slots=True)
class NodeExecutionResult:
    artifact: ArtifactDraft
    acknowledged_mail_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionBatch:
    completed_artifacts: tuple[Artifact, ...]
    failed_node_ids: tuple[str, ...]


class AgentNodeWorker(Protocol):
    async def execute(self, execution: NodeExecutionInput) -> NodeExecutionResult: ...


class AgentGraphStore(Protocol):
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
    ) -> tuple[GraphRecord, AgentNode]: ...

    async def get_graph(self, graph_id: str) -> GraphRecord: ...

    async def find_graph_by_root_run(self, root_run_id: str) -> GraphRecord | None: ...

    async def get_node(self, node_id: str) -> AgentNode: ...

    async def list_nodes(self, graph_id: str) -> list[AgentNode]: ...

    async def spawn_child(
        self,
        graph_id: str,
        parent_node_id: str,
        spec: NodeSpec,
        *,
        ready: bool,
        now_ms: int,
    ) -> AgentNode: ...

    async def activate_node(self, node_id: str, *, now_ms: int) -> AgentNode: ...

    async def lease_ready(
        self, graph_id: str, *, worker_id: str, now_ms: int, lease_ms: int, limit: int
    ) -> list[AgentNode]: ...

    async def complete_node(
        self,
        node_id: str,
        *,
        worker_id: str,
        artifact: ArtifactDraft,
        now_ms: int,
    ) -> Artifact: ...

    async def fail_node(self, node_id: str, *, worker_id: str, now_ms: int) -> None: ...

    async def interrupt_node(self, node_id: str, *, now_ms: int) -> None: ...

    async def cancel_subtree(self, node_id: str, *, now_ms: int) -> list[str]: ...

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
    ) -> MailboxItem: ...

    async def receive_mail(self, node_id: str, *, now_ms: int) -> list[MailboxItem]: ...

    async def acknowledge_mail(self, item_id: str, node_id: str, *, now_ms: int) -> None: ...

    async def acquire_lock(
        self,
        graph_id: str,
        node_id: str,
        target: ResourceTarget,
        *,
        now_ms: int,
        lease_ms: int,
    ) -> bool: ...

    async def release_locks(self, node_id: str) -> None: ...

    async def list_locks(self, node_id: str, *, now_ms: int) -> tuple[ResourceTarget, ...]: ...

    async def owns_write_scope(
        self, owner_id: str, targets: tuple[WriteScopeTarget, ...], *, now_ms: int
    ) -> bool: ...

    async def renew_write_scope(
        self,
        owner_id: str,
        targets: tuple[WriteScopeTarget, ...],
        *,
        now_ms: int,
        lease_ms: int,
    ) -> bool: ...

    async def list_artifacts(self, graph_id: str) -> list[Artifact]: ...

    async def save_checkpoint(
        self, node_id: str, state: dict[str, object], *, now_ms: int
    ) -> AgentCheckpoint: ...

    async def latest_checkpoint(self, node_id: str) -> AgentCheckpoint | None: ...

    async def finish_graph(
        self, graph_id: str, node_id: str, status: GraphStatus, *, now_ms: int
    ) -> None: ...


class MultiAgentSupervisor:
    _delegation: ClassVar[dict[AgentRole, frozenset[AgentRole]]] = {
        AgentRole.ORCHESTRATOR: frozenset(AgentRole),
        AgentRole.AUTHOR: frozenset({AgentRole.VERIFIER, AgentRole.OPERATOR}),
        AgentRole.DATA_ANALYST: frozenset({AgentRole.VERIFIER, AgentRole.OPERATOR}),
        AgentRole.RESEARCHER: frozenset(),
        AgentRole.VERIFIER: frozenset(),
        AgentRole.OPERATOR: frozenset(),
    }

    def __init__(self, store: AgentGraphStore) -> None:
        self._store = store
        self._condition = asyncio.Condition()

    async def create_graph(
        self,
        *,
        root_run_id: str,
        tenant_key: str,
        app_id: str,
        source_resource_kind: str,
        source_resource_id: str,
        agent_definition_id: str,
        agent_definition_version: int,
        root_spec: NodeSpec,
        limits: GraphLimits | None = None,
        now_ms: int,
    ) -> tuple[GraphRecord, AgentNode]:
        selected_limits = limits or GraphLimits()
        if root_spec.role is not AgentRole.ORCHESTRATOR:
            raise ValueError("root node must use the orchestrator role")
        if root_spec.name != "root":
            raise ValueError("root node name must be 'root'")
        result = await self._store.create_graph(
            graph_id=str(uuid4()),
            root_run_id=root_run_id,
            tenant_key=tenant_key,
            app_id=app_id,
            source_resource_kind=source_resource_kind,
            source_resource_id=source_resource_id,
            agent_definition_id=agent_definition_id,
            agent_definition_version=agent_definition_version,
            root_spec=root_spec,
            limits=selected_limits,
            now_ms=now_ms,
        )
        await self._notify()
        return result

    async def spawn(
        self,
        graph_id: str,
        parent_node_id: str,
        spec: NodeSpec,
        *,
        ready: bool = True,
        now_ms: int,
    ) -> AgentNode:
        parent = await self._store.get_node(parent_node_id)
        if parent.graph_id != graph_id:
            raise ValueError("parent does not belong to graph")
        if spec.role not in self._delegation[parent.spec.role]:
            raise PermissionError(
                f"role {parent.spec.role.value} cannot delegate {spec.role.value}"
            )
        if not set(spec.tool_ids).issubset(parent.spec.tool_ids):
            raise PermissionError("child tool authority exceeds parent authority")
        for kind, amount in spec.budgets.items():
            if amount > parent.spec.budgets.get(kind, 0):
                raise PermissionError(f"child {kind} budget exceeds parent budget")
        child = await self._store.spawn_child(
            graph_id, parent_node_id, spec, ready=ready, now_ms=now_ms
        )
        await self._notify()
        return child

    async def activate(self, node_id: str, *, now_ms: int) -> AgentNode:
        node = await self._store.activate_node(node_id, now_ms=now_ms)
        await self._notify()
        return node

    async def send(
        self,
        *,
        graph_id: str,
        sender_node_id: str,
        recipient_node_id: str,
        kind: MailboxKind,
        payload: dict[str, object],
        correlation_id: str | None = None,
        now_ms: int,
    ) -> MailboxItem:
        item = await self._store.send_mail(
            graph_id=graph_id,
            sender_node_id=sender_node_id,
            recipient_node_id=recipient_node_id,
            kind=kind,
            payload=payload,
            correlation_id=correlation_id,
            now_ms=now_ms,
        )
        await self._notify()
        return item

    async def wait_for_mail(
        self, node_id: str, *, now_ms: int, timeout_s: float
    ) -> list[MailboxItem]:
        available = await self._store.receive_mail(node_id, now_ms=now_ms)
        if available:
            return available
        try:
            async with asyncio.timeout(timeout_s):
                async with self._condition:
                    await self._condition.wait()
        except TimeoutError:
            return []
        return await self._store.receive_mail(node_id, now_ms=now_ms)

    async def cancel(self, node_id: str, *, now_ms: int) -> list[str]:
        cancelled = await self._store.cancel_subtree(node_id, now_ms=now_ms)
        await self._notify()
        return cancelled

    async def interrupt(self, node_id: str, *, now_ms: int) -> None:
        await self._store.interrupt_node(node_id, now_ms=now_ms)
        await self._notify()

    async def execute_ready(
        self,
        graph_id: str,
        *,
        worker_id: str,
        worker: AgentNodeWorker,
        now_ms: int,
        lease_ms: int,
    ) -> ExecutionBatch:
        graph = await self._store.get_graph(graph_id)
        nodes = await self._store.lease_ready(
            graph_id,
            worker_id=worker_id,
            now_ms=now_ms,
            lease_ms=lease_ms,
            limit=graph.limits.max_concurrency,
        )

        async def execute_node(node: AgentNode) -> tuple[Artifact | None, str | None]:
            mailbox = tuple(await self._store.receive_mail(node.node_id, now_ms=now_ms))
            artifacts = await self._store.list_artifacts(graph_id)
            if node.parent_node_id is None:
                scoped_artifacts = tuple(artifacts)
            else:
                dependencies = set(node.spec.dependency_node_ids)
                scoped_artifacts = tuple(
                    artifact for artifact in artifacts if artifact.producer_node_id in dependencies
                )
            try:
                result = await worker.execute(NodeExecutionInput(node, mailbox, scoped_artifacts))
            except Exception:
                await self._store.fail_node(node.node_id, worker_id=worker_id, now_ms=now_ms)
                return None, node.node_id
            available_mail_ids = {item.item_id for item in mailbox}
            if not set(result.acknowledged_mail_ids).issubset(available_mail_ids):
                await self._store.fail_node(node.node_id, worker_id=worker_id, now_ms=now_ms)
                return None, node.node_id
            artifact = await self._store.complete_node(
                node.node_id,
                worker_id=worker_id,
                artifact=result.artifact,
                now_ms=now_ms,
            )
            for item_id in result.acknowledged_mail_ids:
                await self._store.acknowledge_mail(item_id, node.node_id, now_ms=now_ms)
            return artifact, None

        results = await asyncio.gather(*(execute_node(node) for node in nodes))
        await self._notify()
        return ExecutionBatch(
            completed_artifacts=tuple(
                artifact for artifact, _failure in results if artifact is not None
            ),
            failed_node_ids=tuple(failure for _artifact, failure in results if failure is not None),
        )

    async def publish_terminal(
        self, graph_id: str, node_id: str, status: RunStatus, *, now_ms: int
    ) -> None:
        mapping = {
            RunStatus.COMPLETED: GraphStatus.COMPLETED,
            RunStatus.BLOCKED: GraphStatus.BLOCKED,
            RunStatus.FAILED: GraphStatus.FAILED,
            RunStatus.CANCELLED: GraphStatus.CANCELLED,
        }
        graph_status = mapping.get(status)
        if graph_status is None:
            raise ValueError("terminal publication requires a terminal status")
        await self._store.finish_graph(graph_id, node_id, graph_status, now_ms=now_ms)
        await self._notify()

    @staticmethod
    def merge_artifacts(artifacts: list[Artifact]) -> ArtifactMerge:
        claims_by_key: dict[str, list[object]] = {}
        for artifact in artifacts:
            claims = artifact.payload.get("claims", {})
            if not isinstance(claims, dict):
                continue
            for key, value in claims.items():
                claims_by_key.setdefault(str(key), []).append(value)
        merged: dict[str, object] = {}
        conflicts: dict[str, tuple[object, ...]] = {}
        for key, values in claims_by_key.items():
            unique: list[object] = []
            for value in values:
                if value not in unique:
                    unique.append(value)
            if len(unique) == 1:
                merged[key] = unique[0]
            else:
                conflicts[key] = tuple(unique)
        return ArtifactMerge(tuple(artifacts), merged, conflicts)

    async def _notify(self) -> None:
        async with self._condition:
            self._condition.notify_all()
