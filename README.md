# Codex2Lark

`Codex2Lark` is a Harness-centered Feishu AI Agent platform. Its current local
plugin combines a focused authoring Skill with semantic MCP tools and uses
`lark-cli` as the interactive Feishu execution backend. The V3 Gateway is a
single-node durable Agent Runtime with a service-native Feishu IM connection,
serving many users and groups independently of Codex or MCP uptime.

Feishu remains the upstream source of truth. Interactive authoring keeps plans
and document-derived state ephemeral. The V3 group Runtime may retain authorized
messages, attachments, run journals, and checkpoints in an encrypted local
mirror with explicit provenance, retention, invalidation, and purge rules.

## Documentation-first status

This repository is documentation driven. The current implementation contract is
defined by:

- [Product requirements](docs/requirements.md)
- [Architecture](docs/architecture.md)
- [Agent Harness](docs/agent-harness.md)
- [Multi-Agent Runtime](docs/multi-agent-runtime.md)
- [Capability plugin contract](docs/runtime-plugins.md)
- [Single-node storage and recovery](docs/single-node-storage.md)
- [Feishu IM plugin](docs/group-agent-runtime.md)
- [V3 design decisions](docs/design-decisions.md)
- [Multi-group architecture research](docs/research/multi-group-agent-architecture.md)
- [MCP tool contracts](docs/mcp-tools.md)
- [Authoring and editing policy](docs/document-authoring.md)
- [Development workflow](docs/development.md)
- [Installation and configuration](docs/operations.md)
- [Usage and shutdown](docs/usage.md)
- [Delivery roadmap](docs/roadmap.md)

No implementation change may precede the documentation change that specifies
it. See [AGENTS.md](AGENTS.md) for the repository-wide rule.

## Target interaction

```text
ChatGPT / Codex
  -> Feishu authoring Skill
  -> Codex2Lark MCP tools
  -> ephemeral compiler and verifier
  -> lark-cli / Feishu OpenAPI
  -> Feishu Docs, Whiteboard, Sheets, Base, and Drive
```

The shipped runtime includes the local stdio MCP server and the V3 Gateway's
durable mention-to-model-to-reply path, bounded multi-Agent delegation, and
verified semantic authoring tools. The V3 implementation and deterministic
acceptance envelope are complete. Each production deployment must still collect
its own opt-in live multi-group and capability read-back evidence; the
[delivery roadmap](docs/roadmap.md) distinguishes implementation completion
from deployment release qualification.

## Quick start

After completing `lark-cli` authentication, prepare and verify the checkout:

```bash
uv sync --all-groups
uv run codex2lark doctor
```

Register the source checkout once from the repository root:

```bash
codex mcp add codex2lark \
  --env UV_CACHE_DIR=/tmp/codex2lark-uv-cache \
  -- uv run --project "$PWD" codex2lark mcp
```

Restart Codex, open a new task, and use `/mcp` to confirm `codex2lark`. Codex
starts and stops the MCP child process automatically; do not manually keep
`uv run codex2lark mcp` running.

After configuring the V3 secrets and state directory described in
[Installation and configuration](docs/operations.md), run the independent
Gateway:

```bash
uv run codex2lark gateway
```

The Gateway uses an outbound long connection and requires neither a public IP
nor RabbitMQ. It durably acknowledges and executes mention-driven root-Agent
tasks; unsupported business actions are not exposed as tools.
See [Usage and shutdown](docs/usage.md) for daily operation and
[Installation and configuration](docs/operations.md) for first-time setup.
