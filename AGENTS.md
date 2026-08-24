# Repository instructions

## Documentation-driven development

Every implementation or behavioral modification MUST begin with a documentation
change in the same workstream.

1. Update the relevant contract in `docs/` before editing implementation code.
2. If no document covers the behavior, add or extend one before coding.
3. Add a short entry to `docs/changes.md` linking the documented decision to the
   implementation area.
4. Implement only behavior described by the updated documentation.
5. Update tests together with the implementation.
6. If implementation reveals a contract problem, stop implementation, revise
   the document, then continue.

Documentation-only research is allowed without code changes. Mechanical
formatting and typo corrections that cannot affect behavior do not require a
change-log entry.

## Project invariants

- Python dependencies and commands are managed with `uv`.
- User document content and document-derived state are never persisted locally.
- Ephemeral files live in a per-request temporary directory and are removed on
  success, failure, cancellation, and timeout.
- Feishu is the only business-data source of truth.
- OAuth credentials are secrets, not business data; they must be provided by
  `lark-cli`, an operating-system keychain, or an external secret provider.
- The model is never exposed to an arbitrary shell or raw `lark-cli` execution
  tool.
- All subprocess calls use argument arrays with `shell=False` semantics.
- External writes require clear user intent and are followed by a live read-back
  verification.

## Validation

Run the documented commands in `docs/development.md` before completing a change.
