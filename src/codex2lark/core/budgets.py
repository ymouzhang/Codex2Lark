from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class BudgetKind(StrEnum):
    TOOL_CALLS = "tool_calls"
    EXTERNAL_WRITES = "external_writes"
    WALL_TIME_MS = "wall_time_ms"
    COST_MICROS = "cost_micros"
    AGENT_NODES = "agent_nodes"


@dataclass(frozen=True, slots=True)
class BudgetLimit:
    kind: BudgetKind
    maximum: int

    def __post_init__(self) -> None:
        if self.maximum < 0:
            raise ValueError("budget maximum cannot be negative")


@dataclass(slots=True)
class BudgetLedger:
    limits: dict[BudgetKind, int]
    consumed: dict[BudgetKind, int] = field(default_factory=dict)
    reserved: dict[BudgetKind, int] = field(default_factory=dict)

    @classmethod
    def from_limits(cls, limits: list[BudgetLimit]) -> BudgetLedger:
        values = {limit.kind: limit.maximum for limit in limits}
        if len(values) != len(limits):
            raise ValueError("budget kinds must be unique")
        return cls(limits=values)

    def available(self, kind: BudgetKind) -> int:
        return self.limits.get(kind, 0) - self.consumed.get(kind, 0) - self.reserved.get(kind, 0)

    def reserve(self, kind: BudgetKind, amount: int) -> None:
        self._validate_amount(amount)
        if amount > self.available(kind):
            raise ValueError(f"{kind.value} budget exceeded")
        self.reserved[kind] = self.reserved.get(kind, 0) + amount

    def consume(self, kind: BudgetKind, amount: int, *, from_reservation: bool = False) -> None:
        self._validate_amount(amount)
        if from_reservation:
            reserved = self.reserved.get(kind, 0)
            if amount > reserved:
                raise ValueError(f"{kind.value} reservation exceeded")
            self.reserved[kind] = reserved - amount
        elif amount > self.available(kind):
            raise ValueError(f"{kind.value} budget exceeded")
        self.consumed[kind] = self.consumed.get(kind, 0) + amount

    def release(self, kind: BudgetKind, amount: int) -> None:
        self._validate_amount(amount)
        reserved = self.reserved.get(kind, 0)
        if amount > reserved:
            raise ValueError(f"{kind.value} reservation release exceeded")
        self.reserved[kind] = reserved - amount

    @staticmethod
    def _validate_amount(amount: int) -> None:
        if amount < 0:
            raise ValueError("budget amount cannot be negative")
