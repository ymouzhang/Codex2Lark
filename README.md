# Codex2Lark

`Codex2Lark` is a stateless, AI-native Feishu authoring plugin for ChatGPT and
Codex. It combines a focused authoring Skill with semantic MCP tools and uses
`lark-cli` as the Feishu execution backend.

The project does not persist document content, block mappings, editing plans, or
artifact copies. Feishu is the source of truth. Each request reads live state,
builds an ephemeral plan, writes with optimistic revision checks where the
platform supports them, verifies the result, and destroys temporary data.

## Documentation-first status

This repository is documentation driven. The current implementation contract is
defined by:

- [Product requirements](docs/requirements.md)
- [Architecture](docs/architecture.md)
- [MCP tool contracts](docs/mcp-tools.md)
- [Authoring and editing policy](docs/document-authoring.md)
- [Development workflow](docs/development.md)
- [Installation and operation](docs/operations.md)
- [Using Codex2Lark from Codex](docs/usage.md)
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

After installing dependencies and completing `lark-cli` authentication, run:

```bash
uv run codex2lark doctor
```

Then register the local stdio server with Codex and use natural-language
authoring requests. See [Using Codex2Lark from Codex](docs/usage.md) for the
registration command, verification steps, prompt examples, tool inventory, and
troubleshooting guidance.
