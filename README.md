# Codex2Lark

`Codex2Lark` is a Harness-centered Feishu AI Agent platform. Its current local
plugin combines a focused authoring Skill with semantic MCP tools and uses
`lark-cli` as the Feishu execution backend. Its V2 architecture adds an
always-on event plane and a reusable Agent Harness so one logical Agent can
serve N Feishu groups without depending on Codex or MCP uptime.

The project does not persist document content, block mappings, editing plans, or
artifact copies. Feishu is the source of truth. Each request reads live state,
builds an ephemeral plan, writes with optimistic revision checks where the
platform supports them, verifies the result, and destroys temporary data.

## Documentation-first status

This repository is documentation driven. The current implementation contract is
defined by:

- [Product requirements](docs/requirements.md)
- [Architecture](docs/architecture.md)
- [Agent Harness](docs/agent-harness.md)
- [Multi-group architecture research](docs/research/multi-group-agent-architecture.md)
- [MCP tool contracts](docs/mcp-tools.md)
- [Authoring and editing policy](docs/document-authoring.md)
- [Development workflow](docs/development.md)
- [安装与配置](docs/operations.md)
- [使用与停止](docs/usage.md)
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

The first deliverable is a local stdio MCP server for Codex. A remote
Streamable HTTP transport can be added later without changing the tool
contracts.

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

Only for real-time Feishu events, run the independent Gateway:

```bash
uv run codex2lark gateway
```

The Gateway uses an outbound long connection and requires neither a public IP
nor RabbitMQ. See [Usage and shutdown](docs/usage.md) for daily operation and
[Installation and configuration](docs/operations.md) for first-time setup.
