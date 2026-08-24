from __future__ import annotations

import hashlib
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RootAgentRollout:
    stable_version: int = 1
    canary_version: int | None = None
    canary_percent: int = 0
    salt: str = ""

    def __post_init__(self) -> None:
        if self.stable_version < 1:
            raise ValueError("stable Agent definition version must be positive")
        if self.canary_percent < 0 or self.canary_percent > 100:
            raise ValueError("canary percentage must be between 0 and 100")
        if self.canary_percent:
            if self.canary_version is None or self.canary_version < 1:
                raise ValueError("enabled canary requires a positive definition version")
            if self.canary_version == self.stable_version:
                raise ValueError("canary definition version must differ from stable")
            if not self.salt.strip():
                raise ValueError("enabled canary requires a rollout salt")

    def select(self, tenant_key: str, app_id: str, chat_id: str) -> int:
        if not all((tenant_key, app_id, chat_id)):
            raise ValueError("rollout selection requires trusted group bindings")
        if not self.canary_percent:
            return self.stable_version
        material = f"{self.salt}\0{tenant_key}\0{app_id}\0{chat_id}".encode()
        bucket = int.from_bytes(hashlib.sha256(material).digest()[:8], "big") % 100
        if bucket < self.canary_percent:
            assert self.canary_version is not None
            return self.canary_version
        return self.stable_version
