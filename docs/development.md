# Development workflow

## 1. Toolchain

- Python 3.12 or newer.
- `uv` for Python version, virtual environment, dependency, lockfile, and command
  management.
- `@larksuite/cli@1.0.89` as the exact supported external runtime dependency;
  `doctor` rejects other versions.
- Official Python MCP SDK for the initial stdio server.
- `pytest`, `ruff`, and `mypy` for validation.

Do not add `requirements.txt`, Poetry, Pipenv, or direct virtualenv management.

## 2. Documentation-first change sequence

For every behavioral change:

1. Update the relevant document under `docs/`.
2. Add an entry to `docs/changes.md`.
3. Update schemas and tests.
4. Implement the change.
5. Run validation.

Pull requests or commits should make the documentation change visible before the
corresponding implementation change in the diff or commit sequence.

## 3. Planned project commands

After the Python project is initialized, the canonical commands are:

```bash
uv sync --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv run codex2lark doctor
uv run codex2lark mcp
```

## 4. Source layout contract

```text
src/codex2lark/
├── cli.py                 local operator commands
├── mcp_server.py          MCP registration and transport
├── models.py              strict public request/response schemas
├── errors.py              stable error taxonomy
├── runtime.py             ephemeral workspace lifecycle
├── lark_cli.py            safe subprocess adapter
├── drive_service.py       live search and managed-folder resolution
├── docs_service.py        document orchestration
├── notification_service.py post-verification bot direct messages
├── chat_membership_service.py bot-visible group membership gate
├── chat_digest_service.py group-message retrieval and digest publishing
├── artifacts_service.py   whiteboard, Sheet, and Base orchestration
├── compiler.py            supported XML construction/preflight
└── verifier.py            live read-back checks
```

The MCP layer remains thin. It validates schemas and delegates to services.

## 5. Testing strategy

### Unit tests

- argv construction for every supported operation;
- strict rejection of extra fields and invalid selectors;
- JSON envelope normalization;
- secret redaction;
- timeout and cancellation handling;
- ephemeral directory cleanup;
- XML escaping and supported-tag preflight;
- verification invariant evaluation.

### Contract tests

A deterministic asyncio subprocess double emits recorded `lark-cli` success
and error envelopes and captures argv/environment/cwd. This tests the adapter
boundary without relying on platform-specific child-watcher behavior. Tests
must not require live Feishu credentials.

### Live integration tests

Live tests are opt-in and require an explicitly configured disposable Feishu
folder. They must never run in the default test command.

The `doctor` command is the operator smoke test for the real executable and
authenticated identity.

## 6. Dependency policy

- Runtime dependencies must have a direct, documented use.
- Versions are resolved and locked by `uv.lock`.
- The external Node.js `@larksuite/cli` dependency is not representable in
  `uv.lock`; its exact version is pinned by the operations install command and
  the runtime compatibility constant checked by `doctor`.
- Subprocess and XML functionality use the standard library where practical.
- The project does not depend on a database, ORM, cache server, or task queue.
- Until the MCP SDK resolves its `Settings.lifespan` forward reference before
  constructing pydantic-settings sources, server startup explicitly rebuilds
  that SDK settings model. This compatibility shim should be removed after an
  SDK upgrade proves the warning is gone.

## 7. Logging

Logs contain operation names, timing, safe resource identifiers, return status,
and Feishu log IDs when available. Logs do not contain document bodies, generated
XML, access tokens, app secrets, or full environment dumps.
