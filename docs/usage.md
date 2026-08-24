# Codex2Lark 使用与停止

本文只说明安装完成后如何在 Codex 中使用 Codex2Lark。首次安装和飞书权限配置见
[安装与配置](operations.md)。

除 Codex 内的 `/mcp` 外，本文命令都在 Codex2Lark 仓库根目录执行。

## 1. 先分清两个进程

| 进程 | 用途 | 谁负责启动 | 是否需要常驻 |
|---|---|---|---|
| `codex2lark mcp` | 让 Codex 创建、查询和修改飞书内容 | Codex 自动启动 | 不需要手工常驻 |
| `codex2lark gateway` | 接收机器人进群等飞书实时事件 | 用户或进程管理器启动 | 使用实时事件时需要 |

日常编辑飞书文档只需要 MCP，不需要启动 Gateway。

## 2. 首次把源码注册到 Codex

如果使用已经安装的 Codex2Lark 插件，跳过本节。插件自带 MCP 配置。

如果直接使用本仓库源码，只注册一次。先进入仓库根目录：

```bash
cd /path/to/codex2lark
```

确认运行环境正常：

```bash
uv run codex2lark doctor
```

注册 MCP：

```bash
codex mcp add codex2lark \
  --env UV_CACHE_DIR=/tmp/codex2lark-uv-cache \
  -- uv run --project "$PWD" codex2lark mcp
```

检查注册结果：

```bash
codex mcp get codex2lark
codex mcp list
```

`codex mcp add` 会把 stdio 启动命令保存到 Codex 本机配置。该命令的参数形式遵循
[OpenAI Docs 的 Codex MCP 命令说明](https://learn.chatgpt.com/docs/developer-commands#codex-mcp)。

注册后重启 Codex，或者新建一个 Codex 任务。不要再单独运行
`uv run codex2lark mcp`。

## 3. 每天如何使用

### 3.1 创建或修改飞书内容

1. 启动 Codex。
2. 新建一个任务。
3. 输入 `/mcp`，确认能看到 `codex2lark`。
4. 直接用自然语言描述要完成的飞书工作。

Codex 会自动启动 MCP 子进程并选择相应工具，不需要手工调用工具名。

创建文档示例：

```text
把我们刚才讨论的方案整理成专业的飞书技术文档。
包含背景、目标、架构图、组件职责表格和实施计划。
创建完成后读取文档进行验证。
```

修改文档示例：

```text
查找“Codex2Lark 架构方案”文档，在部署章节后增加故障恢复方案。
不要修改其他内容。完成后读取验证，并通过飞书机器人通知我修改了什么。
```

整理群聊示例：

```text
把“Codex2Lark 项目群”从 2026-08-20 到 2026-08-24 的消息整理成飞书文档。
按时间和说话人排列，图片插入文档，文件只显示文件名。创建后读取验证。
```

新建的文档、电子表格和多维表格默认进入飞书云盘的 `Codex2Lark` 文件夹。

### 3.2 启用飞书实时事件

只有需要“机器人被拉进群后立即执行”等实时能力时，才单独启动 Gateway：

```bash
uv run codex2lark gateway
```

看到以下日志表示长连接已经就绪：

```text
INFO event gateway ready
```

Gateway 与 Codex、MCP 相互独立。关闭 Codex 不会停止 Gateway。生产环境应使用
systemd、Docker 或其他进程管理器运行该命令。

Gateway 使用飞书长连接，不需要公网 IP、Webhook、RabbitMQ 或数据库。Gateway
退出时，尚未处理的内存任务不会恢复。

## 4. 如何停止

### 停止 MCP

正常情况下不需要手工停止 MCP。它的生命周期由 Codex 管理；关闭或重启 Codex
即可停止或重启相应子进程。结束单个任务后不需要额外操作，但不能把“结束任务”当作
强制停止 MCP 的命令。

如果此前在终端手工运行了下面的命令：

```bash
uv run codex2lark mcp
```

在该终端按 `Ctrl+C`。手工运行只适合协议调试，Codex 不能连接另一个终端中已经
运行的 stdio MCP 进程。

### 停止 Gateway

如果 Gateway 在前台运行，在它所在的终端按 `Ctrl+C`。看到以下日志表示已经停止：

```text
INFO event gateway stopped
```

如果由 systemd 或 Docker 管理，使用对应的服务停止命令。

### 阻止 Codex 再次加载 MCP

删除源码注册：

```bash
codex mcp remove codex2lark
```

确认已经删除：

```bash
codex mcp list
```

删除注册不会删除仓库、飞书授权或已创建的飞书文档。插件模式的卸载方式见
[安装与配置](operations.md#6-停止和卸载)。

## 5. 常见问题

### 运行 `codex2lark mcp` 后终端没有反应

这是正常的。它正在等待 MCP stdio 协议输入。按 `Ctrl+C` 停止，然后按第 2 节把
启动命令注册给 Codex。

### `/mcp` 中没有 `codex2lark`

依次执行：

```bash
codex mcp get codex2lark
uv run codex2lark doctor
```

如果均正常，重启 Codex 或新建任务。不要同时安装插件又手工注册同一个 MCP。

### MCP 启动失败

确认注册命令中的 `--project` 是仓库绝对路径，并在仓库中运行：

```bash
uv sync --all-groups
uv run codex2lark doctor
```

### Gateway 启动失败

先确认飞书事件和权限已经发布，再运行连接探针：

```bash
lark-cli event consume im.chat.member.bot.added_v1 --as bot --timeout 2s
```

正常结果应包含：

```text
[event] ready event_key=im.chat.member.bot.added_v1
[source] feishu-websocket: connected
```

详细的飞书后台配置见[安装与配置](operations.md#5-配置并启动-gateway)。
