from __future__ import annotations

from pathlib import Path

from codex2lark.runtime.tools import (
    PolicyDecision,
    ToolContext,
    ToolExecutor,
    ToolReconciliation,
    ToolRegistry,
)
from codex2lark.runtime.types import (
    ToolCall,
    ToolDefinition,
    ToolEffect,
    VerificationRecord,
    VerificationState,
)
from codex2lark.storage.crypto import EnvelopeCipher, MasterKey
from codex2lark.storage.database import SQLiteDatabase
from codex2lark.storage.runtime_store import RuntimeStore


class Allow:
    async def authorize(self, definition, call, context) -> PolicyDecision:
        del definition, call, context
        return PolicyDecision(True, "allowed")


class NoApproval:
    async def request(self, definition, call, context) -> bool:
        del definition, call, context
        return False


class WriteTool:
    checkpoint_safe_observation = False
    definition = ToolDefinition(
        "feishu.test.write",
        1,
        "write and verify",
        {"type": "object", "properties": {"value": {"type": "string"}}},
        ToolEffect.WRITE,
    )

    def __init__(self, *, fail: bool = False, reconcile_verified: bool = False) -> None:
        self.fail = fail
        self.reconcile_verified = reconcile_verified
        self.execute_calls = 0
        self.reconcile_calls = 0

    def validate(self, arguments: dict[str, object]) -> None:
        if not isinstance(arguments.get("value"), str):
            raise ValueError("value is required")

    async def execute(self, arguments, context) -> dict[str, object]:
        del arguments, context
        self.execute_calls += 1
        if self.fail:
            raise TimeoutError("ambiguous timeout")
        return {"resource": {"token": "resource-1"}}

    async def verify(self, arguments, observation, context) -> VerificationRecord:
        del arguments, observation, context
        return VerificationRecord(
            VerificationState.VERIFIED,
            "test.read_back",
            "verified",
            ("resource-1",),
        )

    async def reconcile(self, arguments, context) -> ToolReconciliation:
        del arguments, context
        self.reconcile_calls += 1
        if self.reconcile_verified:
            return ToolReconciliation(
                {"resource": {"token": "resource-1"}},
                VerificationRecord(
                    VerificationState.VERIFIED,
                    "test.reconcile",
                    "found intended effect",
                    ("resource-1",),
                ),
            )
        return ToolReconciliation(
            {},
            VerificationRecord(
                VerificationState.UNCERTAIN,
                "test.reconcile",
                "effect remains ambiguous",
            ),
        )


def context() -> ToolContext:
    return ToolContext("run", "/root", "tenant", "app", "actor", "session", "bot", 1)


async def setup(tmp_path: Path):
    database = SQLiteDatabase(tmp_path / "runtime.db")
    await database.open()
    store = RuntimeStore(database, EnvelopeCipher(MasterKey("test", b"k" * 32)))
    return database, store


async def test_completed_write_is_reused_without_second_external_call(tmp_path: Path) -> None:
    database, store = await setup(tmp_path)
    clock = [100]
    tool = WriteTool()
    registry = ToolRegistry([tool])
    executor = ToolExecutor(
        registry, Allow(), NoApproval(), store, clock_ms=lambda: clock[0], claim_lease_ms=10
    )
    try:
        first = await executor.execute(
            ToolCall("call-1", tool.definition.tool_id, {"value": "same"}), context()
        )
        clock[0] = 200
        replay = await executor.execute(
            ToolCall("call-2", tool.definition.tool_id, {"value": "same"}), context()
        )

        assert first.succeeded and replay.succeeded
        assert tool.execute_calls == 1
        assert replay.observation["idempotent_replay"] is True
        assert replay.verification.resource_refs == ("resource-1",)
    finally:
        await database.close()


async def test_ambiguous_write_is_reconciled_after_claim_expiry(tmp_path: Path) -> None:
    database, store = await setup(tmp_path)
    clock = [100]
    failing = WriteTool(fail=True)
    first_registry = ToolRegistry([failing])
    first = ToolExecutor(
        first_registry,
        Allow(),
        NoApproval(),
        store,
        clock_ms=lambda: clock[0],
        claim_lease_ms=10,
    )
    try:
        failed = await first.execute(
            ToolCall("call-1", failing.definition.tool_id, {"value": "same"}), context()
        )
        assert failed.error_code == "timeout_error"

        clock[0] = 110
        recovered_tool = WriteTool(reconcile_verified=True)
        registry = ToolRegistry([recovered_tool])
        recovered = ToolExecutor(
            registry,
            Allow(),
            NoApproval(),
            store,
            clock_ms=lambda: clock[0],
            claim_lease_ms=10,
        )
        result = await recovered.execute(
            ToolCall("call-2", recovered_tool.definition.tool_id, {"value": "same"}), context()
        )

        assert result.succeeded
        assert recovered_tool.reconcile_calls == 1
        assert recovered_tool.execute_calls == 0
    finally:
        await database.close()


async def test_inconclusive_reconciliation_blocks_blind_replay(tmp_path: Path) -> None:
    database, store = await setup(tmp_path)
    clock = [100]
    failing = WriteTool(fail=True)
    registry = ToolRegistry([failing])
    executor = ToolExecutor(
        registry, Allow(), NoApproval(), store, clock_ms=lambda: clock[0], claim_lease_ms=10
    )
    try:
        await executor.execute(
            ToolCall("call-1", failing.definition.tool_id, {"value": "same"}), context()
        )
        clock[0] = 110
        uncertain = WriteTool()
        retry = ToolExecutor(
            ToolRegistry([uncertain]),
            Allow(),
            NoApproval(),
            store,
            clock_ms=lambda: clock[0],
            claim_lease_ms=10,
        )

        result = await retry.execute(
            ToolCall("call-2", uncertain.definition.tool_id, {"value": "same"}), context()
        )

        assert result.error_code == "ambiguous_external_effect"
        assert result.verification.state is VerificationState.UNCERTAIN
        assert uncertain.execute_calls == 0
    finally:
        await database.close()
