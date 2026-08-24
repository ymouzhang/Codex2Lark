# Edit an existing Feishu document

## Inspect before editing

Call `feishu_docs_inspect` with XML and `full` detail. Record the current revision
and resolve the target against the live content.

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

After block replacement or deletion, refetch before reusing block IDs. Verify
the requested change and include protected text fragments from unrelated areas
when the user asked that other content remain unchanged.
