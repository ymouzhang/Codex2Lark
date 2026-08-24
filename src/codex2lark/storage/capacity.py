from __future__ import annotations

import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class StoragePressure(StrEnum):
    NORMAL = "normal"
    WARNING = "warning"
    HARD = "hard"


@dataclass(frozen=True, slots=True)
class StorageCapacityPolicy:
    maximum_managed_bytes: int = 10 * 1024 * 1024 * 1024
    minimum_free_bytes: int = 512 * 1024 * 1024
    warning_percent: int = 80
    hard_percent: int = 90

    def __post_init__(self) -> None:
        if self.maximum_managed_bytes < 1 or self.minimum_free_bytes < 0:
            raise ValueError("storage byte limits are invalid")
        if not 1 <= self.warning_percent < self.hard_percent <= 100:
            raise ValueError("storage pressure percentages are invalid")

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> StorageCapacityPolicy:
        values = os.environ if environment is None else environment
        return cls(
            maximum_managed_bytes=cls._integer(
                values, "CODEX2LARK_STORAGE_MAX_BYTES", 10 * 1024 * 1024 * 1024
            ),
            minimum_free_bytes=cls._integer(
                values, "CODEX2LARK_STORAGE_MIN_FREE_BYTES", 512 * 1024 * 1024
            ),
        )

    @staticmethod
    def _integer(values: Mapping[str, str], name: str, default: int) -> int:
        raw = values.get(name)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError(f"environment variable must be an integer: {name}") from exc
        if value < 0:
            raise ValueError(f"environment variable must not be negative: {name}")
        return value


@dataclass(frozen=True, slots=True)
class StorageCapacitySnapshot:
    pressure: StoragePressure
    managed_bytes: int
    maximum_managed_bytes: int
    filesystem_free_bytes: int

    @property
    def permits_download(self) -> bool:
        return self.pressure is not StoragePressure.HARD


class StorageCapacityMonitor:
    def __init__(self, data_dir: Path, policy: StorageCapacityPolicy) -> None:
        if not data_dir.is_absolute():
            raise ValueError("data directory must be absolute")
        self._data_dir = data_dir.resolve()
        self._policy = policy

    def snapshot(self, *, requested_bytes: int = 0) -> StorageCapacitySnapshot:
        if requested_bytes < 0:
            raise ValueError("requested storage allocation cannot be negative")
        self._data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        managed = self._managed_bytes()
        free = shutil.disk_usage(self._data_dir).free
        projected = managed + requested_bytes
        hard_bytes = self._policy.maximum_managed_bytes * self._policy.hard_percent // 100
        warning_bytes = self._policy.maximum_managed_bytes * self._policy.warning_percent // 100
        if projected > hard_bytes or free - requested_bytes < self._policy.minimum_free_bytes:
            pressure = StoragePressure.HARD
        elif projected > warning_bytes:
            pressure = StoragePressure.WARNING
        else:
            pressure = StoragePressure.NORMAL
        return StorageCapacitySnapshot(
            pressure,
            managed,
            self._policy.maximum_managed_bytes,
            free,
        )

    def _managed_bytes(self) -> int:
        total = 0
        for name in ("runtime.db", "runtime.db-wal", "runtime.db-shm"):
            path = self._data_dir / name
            if path.is_file():
                total += path.stat().st_size
        blob_root = self._data_dir / "blobs"
        if blob_root.is_dir():
            total += sum(
                path.stat().st_size for path in blob_root.glob("*/*.blob") if path.is_file()
            )
        return total
