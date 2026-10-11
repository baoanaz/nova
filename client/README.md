# nova-client（Rust，MCP 最终形态）

stdio MCP 客户端 + 本地同步代理（Module/05；MCP 最终形态，D-39 / R38）。

```text
编辑器（Claude Code / Codex / Cursor）
   │ stdio JSON-RPC 2.0
   ▼
nova-client ── 本地：扫描 / 三层忽略 / CF-02 blob_hash / 增量对账 / 缓存 / checkpoint
   │ HTTPS + Bearer
   ▼
nova-service（VPS）── 检索 / 组装 / 渲染（D-21：渲染只在服务端）
```

- 两个工具：`search_context` / `ask_project`（CF-06 冻结；不暴露底层检索工具，D-12）
- 懒同步：每次 tool call 自动保证工作区新鲜（D-27），无常驻 watcher
- 忽略三层：`.novaignore` > `.gitignore`（`ignore` crate 真实解析）> 内置默认（D-28 / R42）
- 客户端零业务智能：切片/检索/渲染全在服务端（D-02 / D-34）

## 构建

```bash
cd client
cargo build --release          # 产物：target/release/nova-client
cargo test                     # 33 个单元测试（含跨语言身份/blob 常量表）
```

## 编辑器配置（Claude Code / Codex 等 stdio harness）

```jsonc
{
  "mcpServers": {
    "nova": {
      "command": "/绝对路径/client/target/release/nova-client",
      "env": { "NOVA_BASE_URL": "http://127.0.0.1:8787" }
    }
  }
}
```

云端（M2c）：`NOVA_BASE_URL=https://nova.example.com` + `NOVA_API_TOKEN=<token>`。

命令行参数与环境变量一一对应：`--base-url`/`NOVA_BASE_URL`、`--token`/`NOVA_API_TOKEN`、
`--cache-root`/`NOVA_CLIENT_CACHE`（默认 `~/.cache/nova`）。

## 自动预热与工作区索引

完成 `initialize` + `notifications/initialized` 后，客户端在后台通过 WorkspaceResolver
定位并预热：`NOVA_PROJECT_DIR` → `CLAUDE_PROJECT_DIR` → 经过验证的 MCP 进程 CWD。
显式路径必须为有效绝对目录；错误或空的显式路径不回退到其他目录。无显式路径时，CWD
需要 Git、项目标记（如 `README.md` / `AGENTS.md` / 项目 manifest）或直接 Git 子项目证据。
自动预热拒绝文件系统根、Home、系统与 Agent/插件/依赖缓存目录；不确定时跳过，首次工具
调用仍按 `project_root` 懒同步。路径验证同样在后台，不卡住初始化、`ping` 或 `tools/list`。

已在官方 Codex CLI 0.162.1 实测普通交互启动、`codex -C /path/to/project`、`codex exec`、
exec `-C` 及 `npx` 包装：MCP 子进程 CWD 正确，Nova 无工作区变量也能自动上传并完成
checkpoint，不需要 SessionStart Hook 或 Agent 主动调用工具。IDE 扩展与 Plugin MCP 未实测，
不能保证其 CWD 指向用户工作区；有需要时可显式设置 `NOVA_PROJECT_DIR`。新版需先发布才能
由 `npx --yes --prefer-online @baoanaz/nova-client@latest` 使用；npm 包名采用作用域以避免同名冲突，二进制仍叫 `nova-client`。

- Git（包括没有 Remote 的 `git init`）及 Linked Worktree：沿用历史项目身份和完整/增量索引，
  复用现有跨项目 Embedding Cache。相同身份的不同可写 checkout 在同缓存根下自动隔离冲突。
- 非 Git：默认仅 `.md` / `.markdown`；隐藏目录同样处理，仍遵守忽略、安全数量/大小边界。
- 多项目容器：分别同步 Git 根和仓库外文档；查询标注来源融合，Git 内文件不重复进文档项目。
- 单项目 `ask_project` 保持原链路；多项目回答透明降级为融合检索证据，不触发 N 次总结模型调用。

配置：`NOVA_AUTO_WARMUP=off` 关闭初始化预热；`NOVA_NON_GIT_INDEX=full` 显式启用非 Git
代码索引；`NOVA_WARMUP_TIMEOUT_SECS` 默认 600、`NOVA_WARMUP_READY_TTL_SECS` 默认 60。
`NOVA_DEVICE_ID` 可覆盖设备标识；`NOVA_WORKSPACE_ISOLATION=1` 可强制隔离 Git checkout。
跨进程任务共享要求相同 `NOVA_CLIENT_CACHE`；文档/隔离路由要求服务端支持 `clientScope`。
构建需 Rust 1.89+（标准库文件锁），MCP 两个工具的参数不变。

最终设计和协调边界见 [MCP 异步预热与工作区智能索引](../docs/design/auto-warmup-workspace.md)。

## 手工核对

```bash
# 我的仓库会映射到服务端的哪个 projectId？（同步问题的第一步排查）
cargo run --example identity -- /path/to/repo

# 与 core 对照（两者必须逐字节一致）
uv run python -c "from nova_core.engine import repo_identity, project_id_for as p; \
  i=repo_identity('/path/to/repo'); print(i.identity_key, p(i.identity_key))"
```

## 与 HTTP 形态（TASK-040）的关系

| 形态 | 传输 | 适用 | 状态 |
|---|---|---|---|
| TASK-040（service 直出） | Streamable HTTP `/mcp` | 支持 HTTP 的编辑器（Cursor） | 已验证，**短期验证用** |
| **本目录（TASK-040R）** | stdio + 远端 HTTPS | Claude Code / Codex 等 stdio harness | **最终形态** |

两者共用同一套服务端检索与渲染；区别只在"谁来做同步"：本地模式由服务端懒重扫（TASK-034 §C），
本形态由 client 在每次 tool call 前增量上传（D-27 的远端分支）。

## 已知边界（勿当 bug）

1. **服务端鉴权尚未实现**（TASK-060/061）：`--token` 会发送，但服务端当前放行所有请求；
   上云前必须完成鉴权（见 `docs/evidence/task-051-cloud-mcp-readiness.md` A1）。
2. **D-29 身份未做协议形式归一化**：`git@host:path` 与 `https://host/path` 得到**两个** projectId
   （就绪度报告 A3，属 L3）。
3. **`ignore` crate 豁免 git 已跟踪文件**，core 的 Python 实现不豁免（约 1.9% 差异，R42 要求前者语义）。
4. **后台预热不等于首次检索零等待**：尚未就绪的工具调用等待共享索引任务；MCP 其他请求照常响应。
5. **stale-blob 自愈分支未实现**（服务端当前不返回 stale 列表，属 checkpoint 语义澄清的一部分）。
