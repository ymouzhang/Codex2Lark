from __future__ import annotations

import hashlib
import unicodedata

from .tools import WriteScopeTarget


def logical_reservation(resource_type: str, value: str) -> WriteScopeTarget:
    if not resource_type or not value.strip():
        raise ValueError("logical reservation identity is required")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).casefold()
    digest = hashlib.sha256(f"{resource_type}\0{normalized}".encode()).hexdigest()
    return WriteScopeTarget(resource_type, f"logical:{digest}")


def exact_target(resource_type: str, *parts: str) -> WriteScopeTarget:
    if not resource_type or not parts or any(not item.strip() for item in parts):
        raise ValueError("exact write target identity is required")
    return WriteScopeTarget(resource_type, ":".join(parts))
