from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TaskConcurrencyLimits:
    global_limit: int
    tenant_limit: int
    app_limit: int
    group_limit: int

    def __post_init__(self) -> None:
        values = (
            self.group_limit,
            self.app_limit,
            self.tenant_limit,
            self.global_limit,
        )
        if min(values) < 1:
            raise ValueError("task concurrency limits must be positive")
        if tuple(sorted(values)) != values:
            raise ValueError(
                "task concurrency limits must satisfy group <= app <= tenant <= global"
            )
