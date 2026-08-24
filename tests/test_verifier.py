from __future__ import annotations

from codex2lark.authoring.verifier import extract_revision, verify_document
from codex2lark.core.models import VerificationPolicy

DATA = {
    "document": {
        "title": "Plan",
        "revision_id": 9,
        "content": "<title>Plan</title><h1>Overview</h1><p>Keep me</p>",
    }
}


def test_verifier_checks_text_title_and_blocks() -> None:
    result = verify_document(
        DATA,
        VerificationPolicy(
            expected_title="Plan",
            required_text=["Overview"],
            protected_text=["Keep me"],
            forbidden_text=["Delete me"],
            min_blocks={"h1": 1},
        ),
    )
    assert result.status == "passed"
    assert extract_revision(DATA) == 9


def test_verifier_reports_failure() -> None:
    result = verify_document(DATA, VerificationPolicy(required_text=["Missing"]))
    assert result.status == "failed"
