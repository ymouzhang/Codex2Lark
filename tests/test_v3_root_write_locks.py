from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from codex2lark.runtime.multi_agent import (
    AgentRole,
    ContextMode,
    GraphLimits,
    MultiAgentSupervisor,
    NodeSpec,
)
from codex2lark.runtime.tools import (
    PolicyDecision,
    ToolContext,
    ToolExecutor,
    ToolReconciliation,
    ToolRegistry,
    WriteScopeTarget,
)
from codex2lark.runtime.types import (
    ToolCall,
    ToolDefinition,
    ToolEffect,
    VerificationRecord,
    VerificationState,
)
from codex2lark.storage.agent_store import SQLiteAgentGraphStore
from codex2lark.storage.crypto import EnvelopeCipher, MasterKey
from codex2lark.storage.database import SQLiteDatabase


class Allow:
    async def authorize(self, definition, call, context) -> PolicyDecision:  # type: ignore[no-untyped-def]
        del definition, call, context
        return PolicyDecision(True, "test")


class NoApproval:
    async def request(self, definition, call, context) -> bool:  # type: ignore[no-untyped-def]
        del definition, call, context
        return False


class BlockingWriteTool:
    checkpoint_safe_observation = False
    definition = ToolDefinition(
        "test.write",
        1,
        "Write one canonical resource.",
        {
            "type": "object",
            "properties": {
                "resource": {"type": "string"},
                "hold": {"type": "boolean"},
            },
            "required": ["resource", "hold"],
            "additionalProperties": False,
        },
        ToolEffect.WRITE,
    )

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.calls: list[str] = []

    def validate(self, arguments: dict[str, object]) -> None:
        if (
            set(arguments) != {"resource", "hold"}
            or not isinstance(arguments["resource"], str)
            or not isinstance(arguments["hold"], bool)
        ):
            raise ValueError("resource and hold are required")

    async def resolve_write_target(
        self, arguments: dict[str, object], context: ToolContext
    ) -> WriteScopeTarget:
        del context
        return WriteScopeTarget("docx", str(arguments["resource"]), "r1")

    async def execute(
        self, arguments: dict[str, object], context: ToolContext
    ) -> dict[str, object]:
        del context
        resource = str(arguments["resource"])
        self.calls.append(resource)
        if arguments["hold"] is True:
            self.started.set()
            await self.release.wait()
        return {"resource": {"reference": resource}}

    async def verify(
        self,
        arguments: dict[str, object],
        observation: dict[str, object],
        context: ToolContext,
    ) -> VerificationRecord:
        del observation, context
        return VerificationRecord(
            VerificationState.VERIFIED,
            "test.readback",
            "verified",
            (str(arguments["resource"]),),
        )

    async def reconcile(
        self, arguments: dict[str, object], context: ToolContext
    ) -> ToolReconciliation:
        del arguments, context
        return ToolReconciliation(
            {}, VerificationRecord(VerificationState.UNCERTAIN, "test", "uncertain")
        )


def root_spec() -> NodeSpec:
    return NodeSpec(
        "root",
        AgentRole.ORCHESTRATOR,
        "Own the result.",
        "AgentOutcome",
        ("test.write",),
        {},
        ContextMode.SELECTED,
    )


async def create_root(
    supervisor: MultiAgentSupervisor,
    *,
    run_id: str,
    source_id: str,
) -> None:
    await supervisor.create_graph(
        root_run_id=run_id,
        tenant_key="tenant",
        app_id="app",
        source_resource_kind="im.message",
        source_resource_id=source_id,
        agent_definition_id="root",
        agent_definition_version=1,
        root_spec=root_spec(),
        limits=GraphLimits(),
        now_ms=1,
    )


def context(run_id: str, task_id: str) -> ToolContext:
    return ToolContext(
        run_id=run_id,
        node_id="/root",
        tenant_key="tenant",
        app_id="app",
        actor_id="user",
        session_key=f"tenant/app/chat/{task_id}",
        identity_ref="user",
        policy_version=1,
        task_id=task_id,
        chat_id="chat",
    )


async def test_root_writes_lock_across_graphs_and_release_exact_target(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = SQLiteAgentGraphStore(database, EnvelopeCipher(MasterKey("test", b"l" * 32)))
    supervisor = MultiAgentSupervisor(store)
    await create_root(supervisor, run_id="run-a", source_id="message-a")
    await create_root(supervisor, run_id="run-b", source_id="message-b")
    tool = BlockingWriteTool()
    registry = ToolRegistry([tool])
    executor = ToolExecutor(
        registry,
        Allow(),
        NoApproval(),
        write_scope_store=store,
        clock_ms=lambda: 10,
    )
    try:
        first = asyncio.create_task(
            executor.execute(
                ToolCall("call-a", "test.write", {"resource": "doc-a", "hold": True}),
                context("run-a", "task-a"),
            )
        )
        await asyncio.wait_for(tool.started.wait(), timeout=1)

        overlap = await executor.execute(
            ToolCall("call-b", "test.write", {"resource": "doc-a", "hold": False}),
            context("run-b", "task-b"),
        )
        disjoint = await executor.execute(
            ToolCall("call-c", "test.write", {"resource": "doc-b", "hold": False}),
            context("run-b", "task-b"),
        )

        assert overlap.error_code == "write_target_busy"
        assert overlap.verification.state is VerificationState.FAILED
        assert disjoint.verification.state is VerificationState.VERIFIED
        assert tool.calls == ["doc-a", "doc-b"]

        tool.release.set()
        assert (await first).verification.state is VerificationState.VERIFIED
        retry = await executor.execute(
            ToolCall("call-d", "test.write", {"resource": "doc-a", "hold": False}),
            context("run-b", "task-b"),
        )
        assert retry.verification.state is VerificationState.VERIFIED
        assert tool.calls == ["doc-a", "doc-b", "doc-a"]
    finally:
        await database.close()


async def test_cancelled_root_write_releases_lock_before_propagating(
    tmp_path: Path,
) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = SQLiteAgentGraphStore(database, EnvelopeCipher(MasterKey("test", b"c" * 32)))
    supervisor = MultiAgentSupervisor(store)
    await create_root(supervisor, run_id="run-a", source_id="message-a")
    await create_root(supervisor, run_id="run-b", source_id="message-b")
    tool = BlockingWriteTool()
    registry = ToolRegistry([tool])
    executor = ToolExecutor(
        registry,
        Allow(),
        NoApproval(),
        write_scope_store=store,
        clock_ms=lambda: 10,
    )
    try:
        active = asyncio.create_task(
            executor.execute(
                ToolCall("call-a", "test.write", {"resource": "doc-a", "hold": True}),
                context("run-a", "task-a"),
            )
        )
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active

        result = await executor.execute(
            ToolCall("call-b", "test.write", {"resource": "doc-a", "hold": False}),
            context("run-b", "task-b"),
        )
        assert result.verification.state is VerificationState.VERIFIED
    finally:
        await database.close()


async def test_root_write_renews_short_lease_through_verification(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = SQLiteAgentGraphStore(database, EnvelopeCipher(MasterKey("test", b"r" * 32)))
    supervisor = MultiAgentSupervisor(store)
    await create_root(supervisor, run_id="run-a", source_id="message-a")
    await create_root(supervisor, run_id="run-b", source_id="message-b")
    tool = BlockingWriteTool()
    registry = ToolRegistry([tool])
    executor = ToolExecutor(
        registry,
        Allow(),
        NoApproval(),
        write_scope_store=store,
        write_lock_lease_ms=30,
        clock_ms=lambda: int(time.monotonic() * 1_000),
    )
    try:
        active = asyncio.create_task(
            executor.execute(
                ToolCall("call-a", "test.write", {"resource": "doc-a", "hold": True}),
                context("run-a", "task-a"),
            )
        )
        await asyncio.wait_for(tool.started.wait(), timeout=1)
        await asyncio.sleep(0.08)

        overlap = await executor.execute(
            ToolCall("call-b", "test.write", {"resource": "doc-a", "hold": False}),
            context("run-b", "task-b"),
        )
        assert overlap.error_code == "write_target_busy"

        tool.release.set()
        assert (await active).verification.state is VerificationState.VERIFIED
    finally:
        await database.close()


async def test_root_lock_rejects_tenant_binding_mismatch(tmp_path: Path) -> None:
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = SQLiteAgentGraphStore(database, EnvelopeCipher(MasterKey("test", b"t" * 32)))
    supervisor = MultiAgentSupervisor(store)
    await create_root(supervisor, run_id="run-a", source_id="message-a")
    try:
        with pytest.raises(PermissionError, match="tenant binding"):
            await store.acquire_root_write_scope(
                "run-a",
                "other-tenant",
                WriteScopeTarget("docx", "doc-a"),
                now_ms=10,
                lease_ms=100,
            )
    finally:
        await database.close()
