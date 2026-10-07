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
4. **首同步在大仓库内同步阻塞**（D-31 的 120s 转后台未做，属后续卡）。
5. **stale-blob 自愈分支未实现**（服务端当前不返回 stale 列表，属 checkpoint 语义澄清的一部分）。
