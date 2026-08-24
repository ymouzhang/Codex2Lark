# Edit an existing Feishu document

## Inspect before editing

If the user gives a title instead of a Feishu URL/token, call
`feishu_docs_search`. Continue automatically only when it returns one exact
candidate. For zero candidates, report that the document was not found; for
multiple candidates, show compact title/link details and ask the user to choose.
Never select a similarly named document by rank or recency.

Call `feishu_docs_inspect` with XML and `full` detail. Record the current
revision and resolve the target against the live content. The edit request may
use either the resolved resource reference or the exact `document_title`, but
not both.

If a heading, phrase, or structural selector matches more than one location,
stop and obtain a more precise target. Do not choose a match by position alone
unless the user explicitly identified that position.

## Choose the smallest operation

Preference order:

1. exact `str_replace`;
2. `block_insert_after`;
3. `block_replace`;
4. `append`;
5. `block_delete` when explicitly authorized;
6. `overwrite` only when the user clearly asked to replace the whole document.

Pass the inspected revision as `expected_revision` when available. If the tool
reports a conflict or best-effort revision warning, refetch before continuing.

Supply `change_summary` as a concise, factual description suitable for a direct
message to the current user. Do not include document bodies, secrets, generated
markup, or speculative claims.

After block replacement or deletion, refetch before reusing block IDs. Verify
the requested change and include protected text fragments from unrelated areas
when the user asked that other content remain unchanged.

After verification the tool attempts an idempotent bot direct message. Report
`notification.status`. If it is `failed`, explain the delivery warning and do
not replay the edit merely to send another message.
