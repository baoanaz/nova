# 本地部署指南

[文档中心](../../README.md) · [项目首页](../../../README.md)

本指南在 Linux / WSL 中运行一套真实 nova：Python 后端、React UI 和可供 Agent 使用的 MCP 客户端。Windows 用户可以把服务端放在 WSL 中，客户端使用 Windows npm 包。

完成后：

| 入口 | 地址 | 用途 |
|---|---|---|
| Web UI | `http://127.0.0.1:5173` | 初始化账户、管理项目和 API Key、查看历史、配置 LLM |
| API 服务 | `http://127.0.0.1:8787` | `nova-client --base-url` 使用的实际后端地址 |
| 公网 UI 演示 | <http://23.159.248.240:8088> | 模拟数据展示，不承担真实索引与 MCP 请求 |

## 1. 环境要求

| 工具 | 要求 |
|---|---|
| Python | 3.12+ |
| uv | 安装与运行 Python workspace |
| Node.js / npm | Web 开发使用 Node.js 22；仅运行 npm 客户端最低为 Node.js 18 |
| Git | 获取代码与识别仓库 |
| Rust stable | 仅修改或从源码构建 Rust 客户端时需要 |

```bash
python3 --version
uv --version
node --version
npm --version
```

首次安装会下载 Python 和 npm 依赖。系统工具和 Rust 工具链的补充说明见 [开发环境清单](../../../ENVIRONMENT.txt)。

## 2. 获取代码与安装依赖

```bash
git clone https://github.com/baoanaz/nova.git nova
cd nova
bash scripts/setup-dev.sh --with-web
```

安装脚本会安装锁定依赖，并在不存在时创建：

- `.env`：本机配置，来自 `.env.example`。
- `$HOME/.key/nova/secrets.env`：仓库外的私有凭据文件，初始 Key 为空。
- `.venv/`、`web/node_modules/`：本机依赖。
- `.local/`：缓存、运行数据和日志等本地产物。

脚本保留已有配置和凭据。密钥不填入 `.env.example` 或提交到 Git。

## 3. 配置 embedding 与 LLM

用本机编辑器打开 `$HOME/.key/nova/secrets.env`，将供应商签发的 Key 填入已有字段：

```dotenv
EMBED_API_KEY=填写你的_embedding_API_Key
ANSWER_API_KEY=填写你的_LLM_API_Key
```

`.env.example` 的当前 embedding 配置为 API 模式、Voyage `voyage-4-lite`、1024 维。真实向量索引需要有效的 `EMBED_API_KEY`；仅启动 UI 和初始化账户不代表索引已可用。API 模式会把待嵌入的代码或文档文本发送给配置的供应商。

LLM 总结是可选项。可以在登录后的 **设置 → 自定义 LLM 总结模型** 中填写模型、接口和 Key；也可以为服务设置 `ANSWER_BASE_URL`、`ANSWER_MODEL`、`ANSWER_API_KEY`，并按服务支持的接口选择 `ANSWER_PROTOCOL`。缺少配置时 `ask_project` 明确降级，`search_context` 不依赖总结 LLM。

在仓库根目录加载配置：

```bash
set -a
source .env
set +a
```

修改配置后，需要在启动后端的终端重新加载并重启服务。模型、维度或数据根的迁移步骤见 [embedding 配置与切换](../operations/embedding-provider切换.md)。

## 4. 启动后端与 UI

终端 A，在已加载 `.env` 的仓库根目录启动后端：

```bash
uv run nova-service serve --host 127.0.0.1 --port 8787
```

终端 B，在仓库根目录启动前端：

```bash
npm --prefix web run dev -- --host 127.0.0.1 --strictPort
```

打开 <http://127.0.0.1:5173>。Vite 会把 `/api` 和 `/healthz` 请求代理到 `127.0.0.1:8787`。若后端改为其他端口，启动前端时指定对应地址：

```bash
NOVA_WEB_API=http://127.0.0.1:8891 \
  npm --prefix web run dev -- --host 127.0.0.1 --strictPort
```

可以在另一个终端检查后端是否响应：

```bash
curl --fail http://127.0.0.1:8787/healthz
curl --fail http://127.0.0.1:8787/api/meta
```

健康响应和页面能打开只说明服务启动成功；模型与索引是否可用，需要后续实际调用确认。

## 5. 初始化账户与创建 API Key

1. 全新数据根首次打开 UI 时，会进入 **初始化账户** 页面。设置账户名和密码，创建首个管理员，无需邀请码。
2. 后续使用 **登录** 页面进入。普通新用户注册需要管理员提供的邀请码。
3. 在 **API Key** 页面创建 Key，保存只展示一次的明文。
4. 在 **设置** 页面配置可选的个人 LLM 总结模型。

`.env.example` 中的 `NOVA_ADMIN_NAME` 用于指定部署管理员名称；全新部署的 bootstrap 本身会把首个账户设为管理员。实际数据位于 `NOVA_DATA_ROOT`，更换这个目录相当于切换另一套账户和索引数据。

## 6. 配置 MCP 接入

```bash
npm install -g nova-client@latest
nova-client --help
```

例如，在 Codex 的 `~/.codex/config.toml` 中填写：

```toml
[mcp_servers.nova]
command = "nova-client"
args = ["--base-url", "http://127.0.0.1:8787", "--token", "<你的 API Key>"]
startup_timeout_ms = 60000
```

也可以使用 UI 的 **接入指南** 生成配置。开发环境中请将页面默认带入的 `5173` 改成实际后端端口 `8787`。Agent 在另一台机器上运行时，`127.0.0.1` 指向那台机器自身，应改为 Agent 能访问的服务地址。

重启 Agent 后确认两个工具可见。在要处理的仓库里发起一次代码问题，首次调用会扫描、上传并建立索引；后续调用走增量同步。其他 Agent 的配置见 [客户端说明](../../../npm/README.md)。

可选：在项目 `AGENTS.md` 中加入这一个短样例：

> 处理代码任务时，优先用 search_context 定位相关实现；需要跨文件解释时用 ask_project。修改前核对证据，任务转向新模块时补充检索，避免凭猜测作答。

## 7. 单人本地模式

只想绑定本机仓库、暂时不需要账户体系时，可以停止普通服务，使用独立数据根启动：

```bash
uv run nova-service local \
  --repo /绝对路径/你的仓库 \
  --data-root .local/local-data \
  --host 127.0.0.1 --port 8787
```

该命令直接读取本机仓库并启动后台索引，仍需要真实 embedding 配置；UI 直接进入，客户端可以省略 `--token`。本地模式没有账户与 API Key 管理，应仅绑定本机回环地址。普通 `serve` 模式默认启用账户鉴权，不能把两种模式的配置混用。

## 8. 停止、重启与常见问题

在前端和后端终端分别按 `Ctrl+C` 停止服务。重启时重新加载 `.env`，使用同一个数据根即可继续使用已有账户和索引。

| 现象 | 检查方式 |
|---|---|
| UI 提示无法连接 | 确认后端已启动，Vite 的 `NOVA_WEB_API` 指向实际端口 |
| 首次索引失败 | 检查 embedding 凭据是否加载、供应商地址和模型是否匹配 |
| MCP 返回 401 | 使用实际服务创建的有效 API Key，不使用 UI 演示站地址 |
| 出现初始化页面但原来有账户 | 检查当前 `NOVA_DATA_ROOT` 是否与原部署一致 |
| `ask_project` 提示未配置 | 配齐总结模型、地址和 Key；核对上游协议 |
| 找不到客户端二进制 | 检查 npm 是否安装了当前平台的 optional dependency，见 [npm 包学习](../getting-started/npm-client.md) |
| 端口被占用 | 停止占用进程或更换端口，并同步 Vite 代理与 MCP 地址 |

生产环境需要构建静态 UI，并通过反向代理托管页面与 API，见 [VPS 部署](vps.md)。本指南的 Vite 开发服务器用于本机开发。
