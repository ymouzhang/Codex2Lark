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

The package follows a responsibility-based `src` layout. The root package
contains only the public version and CLI entrypoint; implementation modules live
in cohesive subpackages.

```text
src/codex2lark/
├── __init__.py             package version only
├── cli.py                  console entrypoint and command selection
├── core/                   shared models, errors, and ephemeral runtime
├── adapters/               external-system adapters such as lark-cli
├── authoring/              document compilation and verification
├── services/               Feishu application use cases
├── realtime/               long connection, queueing, dispatch, and Gateway
└── interfaces/             MCP transport and interactive composition root
```

Every subpackage has an `__init__.py` but does not re-export its implementation
surface. Callers import the defining module explicitly. The former flat module
paths are internal and are removed rather than preserved as forwarding shims.
This prevents duplicate APIs and keeps the root directory small.

Dependency direction is:

```text
interfaces / realtime application -> services -> authoring / adapters / core
realtime source and handlers       -> adapters / services / core
authoring and adapters             -> core
core                               -> Python and third-party libraries only
```

The MCP layer remains thin. It validates schemas and delegates to services. It
does not import the realtime package. Service modules do not import interfaces
or realtime lifecycle code.

### V2 dependency boundaries

The Harness and realtime plane remain separate from client and transport code.
The current V2 Lite slice uses one cohesive package per boundary:

```text
src/codex2lark/
├── realtime/
│   ├── source.py        outbound long-connection adapter
│   ├── delivery.py      TaskQueue port and partitioned dispatcher
│   ├── handlers.py      deterministic event handlers
│   ├── gateway.py       lifecycle coordinator
│   └── application.py   standalone runtime dependency wiring
├── services/            semantic Feishu application services
└── interfaces/
    ├── application.py   interactive dependency wiring
    └── mcp.py           stdio MCP interface
```

The Gateway depends on source, queue, and handler ports. The long-connection and
in-memory implementations satisfy those ports without leaking subprocess or
scheduling details into business handlers. MCP imports no event-runtime module.
A future durable queue implements `TaskQueue`; it must not change event sources
or handlers. A future Harness must not import MCP, Webhook, RabbitMQ, lark-cli
subprocess, or a concrete model SDK.

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
- package-boundary checks that reject application modules in the root package
  and prevent MCP from importing realtime modules.

### Contract tests

A deterministic asyncio subprocess double emits recorded `lark-cli` success
and error envelopes and captures argv/environment/cwd. This tests the adapter
boundary without relying on platform-specific child-watcher behavior. Tests
must not require live Feishu credentials.

### Harness evals

Versioned eval fixtures cover group isolation, normalized message conversion,
context selection, prompt injection, tool authorization, approvals, duplicate
delivery, idempotency, steering/follow-up, compaction, verified completion, and
truthful blocked/failure outcomes. Harness, prompt, policy, tool-schema, or
AgentDefinition changes must report eval deltas before release.

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
- The default profile does not depend on a database, ORM, cache server, external
  task queue, public IP, or inbound Webhook endpoint.
- Until the MCP SDK resolves its `Settings.lifespan` forward reference before
  constructing pydantic-settings sources, server startup explicitly rebuilds
  that SDK settings model. This compatibility shim should be removed after an
  SDK upgrade proves the warning is gone.

## 7. Logging

Logs contain operation names, timing, safe resource identifiers, return status,
and Feishu log IDs when available. Logs do not contain document bodies, generated
XML, access tokens, app secrets, or full environment dumps.
