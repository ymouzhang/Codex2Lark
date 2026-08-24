from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .models import VerificationPolicy


@dataclass(frozen=True, slots=True)
class VerificationResult:
    status: str
    checks: tuple[dict[str, Any], ...]

    def as_dict(self) -> dict[str, Any]:
        return {"status": self.status, "checks": list(self.checks)}


def find_first_value(value: Any, keys: set[str]) -> Any:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and child is not None:
                return child
        for child in value.values():
            found = find_first_value(child, keys)
            if found is not None:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_first_value(child, keys)
            if found is not None:
                return found
    return None


def extract_content(data: dict[str, Any]) -> str:
    value = find_first_value(data, {"content", "raw_content", "markdown", "xml"})
    return value if isinstance(value, str) else ""


def extract_revision(data: dict[str, Any]) -> int | None:
    value = find_first_value(data, {"revision_id", "revision"})
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def extract_resource(data: dict[str, Any]) -> dict[str, Any]:
    document = find_first_value(data, {"document"})
    if isinstance(document, dict):
        return document
    return data


def verify_document(data: dict[str, Any], policy: VerificationPolicy) -> VerificationResult:
    content = extract_content(data)
    title = find_first_value(data, {"title"})
    checks: list[dict[str, Any]] = []

    if policy.expected_title is not None:
        passed = title == policy.expected_title or (
            not isinstance(title, str) and policy.expected_title in content
        )
        checks.append({"check": "expected_title", "passed": passed})

    for text in policy.required_text:
        checks.append({"check": "required_text", "value": text, "passed": text in content})
    for text in policy.protected_text:
        checks.append({"check": "protected_text", "value": text, "passed": text in content})
    for text in policy.forbidden_text:
        checks.append({"check": "forbidden_text", "value": text, "passed": text not in content})
    for block_type, minimum in policy.min_blocks.items():
        pattern = rf"<{re.escape(block_type)}(?:\s|>)"
        count = len(re.findall(pattern, content))
        checks.append(
            {
                "check": "min_blocks",
                "block_type": block_type,
                "minimum": minimum,
                "actual": count,
                "passed": count >= minimum,
            }
        )

    status = "passed" if all(check["passed"] for check in checks) else "failed"
    return VerificationResult(status=status, checks=tuple(checks))
