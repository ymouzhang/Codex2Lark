# Documentation-driven change log

This file records behavioral implementation work and the document that authorized
it. It is not a release changelog.

## 2026-08-24

- Defined the `Codex2Lark` product name and lowercase `codex2lark` machine
  identifier contract before renaming code, packaging, plugin metadata, Skill
  dependencies, tests, caches, and documentation.
- Defined the stateless product scope in `requirements.md` before project code.
- Defined the Skill + MCP + ephemeral compiler architecture in
  `architecture.md` before project code.
- Defined the first-release MCP surface in `mcp-tools.md` before tool
  implementation.
- Defined the supported authoring/editing behavior in `document-authoring.md`
  before compiler and service implementation.
- Defined `uv`, source layout, testing, and validation requirements in
  `development.md` before Python project initialization.
- Added the typed `DocumentSpec` and `feishu_docs_publish` contracts before
  implementing structured rich-document compilation.
- Changed default adapter contract tests to a deterministic asyncio subprocess
  double; real executable/authentication checks remain in `doctor` and opt-in
  integration tests.
- Documented installation, authentication, direct MCP operation, Codex plugin
  wiring, smoke tests, and failure recovery before final runtime packaging.
- Required MCP read/write/destructive annotations before adding them to tool
  registration.
- Documented the ephemeral uv cache used by the bundled MCP child process before
  adding it to runtime configuration.
- Added enforced ambiguity, exact-match, per-operation live snapshot, and block
  ID validation rules before tightening the edit service.
- Documented the temporary MCP SDK settings-model compatibility shim before
  removing its noisy startup warning.
- Defined the bare `lark-cli auth status` response contract and usable-identity
  health rule before correcting adapter normalization and `doctor` reporting.
- Pinned the supported external `@larksuite/cli` runtime to `1.0.89` in the
  installation contract before adding adapter and `doctor` version enforcement.
- Defined the live `Codex2Lark` managed-folder policy, exact title discovery,
  and post-verification bot notification contract before implementing Drive
  resolution, title-targeted editing, and edit completion messages.
- Defined the bounded group-chat digest contract before implementing exact chat
  discovery, chronological sender-aware normalization, selective ephemeral image
  handling, filename-only file entries, managed-folder creation, and read-back
  verification.
