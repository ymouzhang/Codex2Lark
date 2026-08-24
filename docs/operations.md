# Codex2Lark 安装与配置

本文用于首次安装、飞书授权和 Gateway 配置。安装完成后的日常启动与停止见
[使用与停止](usage.md)。

除 lark-cli 安装命令外，本文命令都在 Codex2Lark 仓库根目录执行。

## 1. 环境要求

- Python 3.12 或更高版本；
- `uv`；
- Node.js 和 `npx`；
- Codex；
- 可以创建企业自建应用的飞书账号。

## 2. 安装并登录 lark-cli

Codex2Lark 当前固定使用 `@larksuite/cli@1.0.89`：

```bash
npx @larksuite/cli@1.0.89 install
lark-cli config init
lark-cli auth login --recommend
lark-cli auth status
```

不要使用 `@latest`。飞书凭证由 lark-cli 管理，Codex2Lark 不保存凭证。

## 3. 安装 Python 依赖

进入仓库根目录：

```bash
uv sync --all-groups
uv run codex2lark doctor
```

正常结果应包含：

```json
{
  "ok": true,
  "checks": {
    "lark_cli_version": "1.0.89",
    "business_data_persistence": "disabled"
  }
}
```

如果 `ok` 为 `false`，按照返回的 `next_action` 修复后重新运行。

## 4. 连接 Codex

选择一种方式，不要同时使用。

### 使用源码

按照[使用与停止](usage.md#2-首次把源码注册到-codex)执行一次
`codex mcp add`。之后由 Codex 自动启动 MCP，不需要手工保持
`uv run codex2lark mcp` 运行。

### 使用已安装插件

插件自带 `.mcp.json`，Codex 会自动启动 MCP。安装或更新插件后重启 Codex，并用
`/mcp` 检查工具。不要再添加同名的手工 MCP 注册。

## 5. 配置并启动 Gateway

如果只从 Codex 创建和修改飞书内容，跳过本节。

如果需要机器人进群后立即执行自动化，在飞书开发者后台完成以下配置：

1. 为机器人授予：
   - `im:chat.members:bot_access`
   - `im:chat.members:read`
   - `im:chat.members:write_only`
2. 打开“事件与回调”，添加“机器人被添加至群聊”事件：
   `im.chat.member.bot.added_v1`。
3. 创建并发布新的应用版本。只保存配置不会生效。
4. 确认当前 lark-cli 用户位于应用可用范围内。

先做一次两秒连接探针：

```bash
lark-cli event consume im.chat.member.bot.added_v1 --as bot --timeout 2s
```

正常结果包含：

```text
[event] ready event_key=im.chat.member.bot.added_v1
[source] feishu-websocket: connected
```

停止探针，然后启动 Gateway：

```bash
uv run codex2lark gateway
```

看到 `INFO event gateway ready` 后即可把机器人加入测试群。机器人已经在群里时不会
产生新的进群事件，需要先移除再重新加入。

Gateway 需要访问公网，但不需要公网 IP 或域名。当前默认使用内存队列，停止期间的
事件和退出时尚未完成的任务不会重放。

## 6. 停止和卸载

### 停止前台进程

- 手工运行的 MCP：在对应终端按 `Ctrl+C`；
- Gateway：在对应终端按 `Ctrl+C`；
- Codex 自动启动的 MCP：由 Codex 管理；关闭或重启 Codex 即可停止或重启子进程。

### 删除源码 MCP 注册

```bash
codex mcp remove codex2lark
```

### 卸载插件

先查看插件来源：

```bash
codex plugin list --json
```

然后使用列表中显示的 marketplace 名称：

```bash
codex plugin remove codex2lark@MARKETPLACE
```

删除 MCP 注册或插件不会删除飞书授权、仓库或已创建的飞书资源。

### 退出飞书登录

只有需要撤销本机 lark-cli 登录时才执行：

```bash
lark-cli auth logout
```

## 7. 飞书功能权限

不同操作需要对应的飞书权限。遇到权限错误时，以 lark-cli 返回的缺失 scope 为准。

常用权限包括：

| 功能 | 常用权限 |
|---|---|
| 创建和修改文档 | Docs、Drive、`space:folder:create`、`search:docs:read` |
| 修改完成后发送通知 | `im:message:send_as_bot` |
| 读取群消息并生成汇总 | `im:chat:read`、`im:message:readonly` 和用户消息历史权限 |
| 邀请当前用户进入机器人所在群 | `im:chat.members:read`、`im:chat.members:write_only` |

新建的文档、电子表格和多维表格进入飞书云盘根目录下的 `Codex2Lark` 文件夹。
文件夹由系统按需创建，不在本地保存 folder token。

## 8. 故障排查

- `lark_cli: missing`：重新安装 `@larksuite/cli@1.0.89` 并检查 PATH。
- lark-cli 版本不匹配：重新运行固定版本安装命令，不要使用 `@latest`。
- 身份不可用：运行 `lark-cli auth login --recommend`。
- MCP 已注册但没有工具：重启 Codex 或新建任务，再用 `/mcp` 检查。
- 文档标题不唯一：提供文档 URL，或重命名重复文档。
- `Codex2Lark` 文件夹重复：保留一个准确名称的文件夹后重试。
- 机器人无法邀请用户：检查成员权限、应用可用范围和群邀请策略。
- Gateway 无法启动：确认事件已经随新应用版本发布，并重新运行连接探针。
- 修改成功但通知失败：检查 `im:message:send_as_bot`；不要为了补发通知重复修改文档。
