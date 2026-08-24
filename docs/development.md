# Development workflow

## 1. Toolchain

- Python 3.12 or newer;
- `uv` for environments, dependencies, lockfile, and commands;
- `@larksuite/cli@1.0.89` for the supported transitional adapter;
- `pytest`, `ruff`, and `mypy` for validation;
- standard-library SQLite for the V3 single-node store unless implementation
  evidence justifies one small migration/query dependency.

Do not add `requirements.txt`, Poetry, Pipenv, or manually managed virtualenvs.

## 2. Documentation-first sequence

For every behavioral change:

1. update the relevant normative document under `docs/`;
2. add a short entry to `docs/changes.md`;
3. update schemas, fixtures, tests, and eval expectations;
4. implement only the documented behavior;
5. run validation and record any limitations.

If implementation exposes a contract problem, revise the document before
continuing. V3 may replace existing internals; do not add compatibility shims
unless an approved external migration explicitly requires one.

## 3. Canonical validation

```bash
uv sync --all-groups
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest
uv run codex2lark doctor
```

`doctor` is an operator smoke test and may require local Feishu authentication.
Default unit/contract tests require neither credentials nor network access. Live
tests are opt-in and use disposable Feishu resources.

## 4. V3 target source layout

The target layout follows dependency direction and business capability. It is a
redesign target, not a claim about the current tree.

```text
src/codex2lark/
├── __init__.py
├── cli.py
├── core/                       pure domain types and utilities
│   ├── ids.py
│   ├── events.py
│   ├── errors.py
│   ├── budgets.py
│   └── cancellation.py
├── runtime/                    domain-neutral Agent platform
│   ├── kernel.py
│   ├── plugins.py
│   ├── admission.py
│   ├── scheduler.py
│   ├── supervisor.py
│   ├── harness.py
│   ├── context.py
│   ├── resources.py
│   ├── capabilities.py
│   ├── identity.py
│   ├── policy.py
│   ├── verification.py
│   ├── results.py
│   └── observability.py
├── storage/                    SQLite/encryption/blob implementations
│   ├── database.py
│   ├── migrations/
│   ├── repositories/
│   ├── crypto.py
│   ├── blobs.py
│   └── maintenance.py
├── capabilities/               trusted Feishu plugins
│   ├── im/
│   ├── identity/
│   ├── drive/
│   ├── docs/
│   ├── sheets/
│   ├── base/
│   └── whiteboard/
├── adapters/                   model, Feishu, secret, and clock adapters
├── interfaces/                 CLI, MCP, and future administration adapters
└── bootstrap/                  configuration and composition roots

resources/
├── agents/
├── roles/
├── skills/
├── prompts/
├── policies/
├── response-templates/
└── evals/
```

Shipped runtime resource packages live under the importable
`codex2lark.bundled_resources` package so wheel installation and source-checkout
execution resolve identical bytes. Top-level design assets may remain under
`resources/`, but production composition must use the importable package and
must not depend on the current working directory.

The root package contains only version and CLI entrypoint. Subpackages do not
re-export broad internal surfaces. Interfaces depend on application/runtime
ports, never concrete repositories or plugin internals.

## 5. Dependency rules

```text
interfaces/bootstrap -> runtime ports -> core
runtime               -> core and declared ports
capability plugins    -> runtime plugin API + core
storage/adapters      -> runtime ports + core
core                  -> Python/third-party primitives only
```

Forbidden dependencies include:

- `core` importing runtime, storage, adapters, plugins, or interfaces;
- the Harness importing MCP, CLI, lark-cli, a concrete model SDK, or one Feishu
  domain;
- one capability plugin importing another plugin's private modules or tables;
- event adapters calling models or slow business operations on receive paths;
- models receiving repository, SQL, filesystem, subprocess, raw lark-cli, or
  generic OpenAPI access;
- multiple composition roots constructing different production semantics.

Automated import-boundary tests enforce these rules.

## 6. Implementation discipline

- Prefer immutable value objects and explicit state machines for durable state.
- Keep transactions in application services/repositories, not models or HTTP
  adapters.
- Use argument arrays and `shell=False` for every subprocess.
- Use parameterized SQL and explicit transaction boundaries.
- Inject clock, ID generation, model, Feishu, secrets, encryption, and storage
  ports for deterministic tests.
- Treat cancellation, timeout, duplicate delivery, restart, and partial external
  writes as normal paths.
- Avoid speculative base classes and generic repositories. Extract an interface
  only when a stable responsibility has at least one production adapter and a
  test adapter.
- Keep typed plugin schemas; do not introduce universal JSON/EAV business
  storage.

## 7. Test pyramid

### Unit tests

Pure state machines, budget accounting, admission, context selection,
compaction boundaries, policy, schema validation, idempotency, retention, and
artifact merge.

### Contract tests

- Feishu and model adapters against recorded envelopes;
- storage ports against SQLite and the in-memory test implementation;
- every capability manifest/tool/provider/publisher contract;
- encrypted blob crash stages and cleanup;
- plugin migrations from empty and supported prior schemas.

### Harness and multi-Agent evals

Versioned fixtures cover group isolation, injection, role/context scoping,
delegation limits, mailboxes, parallel/disallowed writes, approvals, restart,
compaction, verification, and truthful outcomes. Harness, AgentDefinition, role,
Skill, prompt, policy, schema, compactor, or model changes report eval deltas.

### Integration and live tests

Local integration tests run the full process with fake Feishu/model services and
real SQLite/encryption. Opt-in live tests use a disposable tenant/folder/group
and always read writes back. Load and chaos suites exercise many groups,
process death, rate limits, provider outage, disk pressure, and restore.

## 8. Dependency policy

- Every runtime dependency has a documented direct purpose.
- Python versions are resolved and locked in `uv.lock`.
- The Node `@larksuite/cli` version is pinned by installation docs and checked
  by `doctor`; V3 may remove this dependency when service-native adapters cover
  every required path.
- The default profile has no RabbitMQ, Redis, PostgreSQL, object store, public
  IP, or inbound Webhook dependency.
- Dependency additions that create a service, daemon, native runtime, or new
  persistence format require an explicit design decision and operations plan.

## 9. Logging and diagnostics

Logs contain typed operation names, trace/run/node IDs, safe resource
identifiers, timings, sizes, state transitions, retry categories, and upstream
request IDs. They exclude message/document/file bodies, generated XML, prompts,
hidden reasoning, access tokens, secrets, encryption keys, and environment
dumps.
