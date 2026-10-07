# Module 05 — MCP 与同步（组件详细设计）

> 系列：nova 组件详细设计（Module/），本文是第 5 篇，入口层。
> 内部双子模块：**MCP 适配层**（薄协议壳）+ **Workspace 同步客户端**（厚实的本地代理）——二者必须分开设计，notace 全部 9 个文件都在后者上（Background/01）。
> 依赖：消费 02-04 的检索/组装/总结能力（经服务端 API）；本地侧只做同步与协议。
> 状态：草案（待评审）。最后更新：2026-09-09。
> 修订记录：2025-09-09 v1 — 覆盖初版（外部 AI 思考稿），吸收其懒同步/checkpoint/两工具边界/schema 简单化（见 §10 对照）；补充 project identity 解析、.gitignore 补齐、freshness 语义、首同步断点续传、分层超时矩阵。
> 修订记录：2026-09-09 v2 — 本地缓存字段遵循 Module/01 D-43：contentHash 使用文件 content hash，blobHashes 使用同步 blob hash。

## 0. 组件定位与产品边界

```text
Codex / Claude Code / Cursor（Harness）
        │ MCP stdio（JSON-RPC 2.0）
        ▼
┌─ nova local client（本组件）─────────────┐
│  MCP 适配层：tools schema / 错误映射 /      │
│             stdout 协议纯净 / stderr 日志   │   ← 薄，无任何检索逻辑
│  同步客户端：scan / ignore / hash / cache / │
│             增量上传 / checkpoint / 自愈     │   ← 厚，notace 思想蓝本
└──────────────┬────────────────────────┘
               │ HTTPS + Bearer（服务端 API）
               ▼
        nova VPS（01 索引 / 02 检索 / 03 组装 / 04 总结）
```

**核心原则**：
> MCP 是 Agent 接口，Local Client 是同步代理，VPS 才是真正的 Context Engine。工具接口表达用户意图，不暴露实现细节。

## 1. 设计约束

| # | 约束 | 来源 |
|---|---|---|
| S1 | V1 只暴露 search_context / ask_project 两个工具 | D-12 |
| S2 | 常规 tool call 自动保证工作区新鲜，用户无需手动 sync（CLI sync 仅供 debug） | D-27 懒同步 |
| S3 | 同步的是**源码文件级 blob**；切片/索引全在服务端（Source 与 Derived Index 分离） | D-01/D-02 |
| S4 | 客户端零业务智能：哪些文件被跳过由服务端裁决（skipped_blobs），客户端尊重执行 | notace 模式（Background/01 §3.4） |
| S5 | 大仓库首同步不得让首个 tool call 无限阻塞 | 单 VPS 实际场景（C/C++ monorepo 十万文件级） |

## 2. MCP 适配层

### 2.1 工具 Schema（正式定义，保持简单——不暴露 rrf_k/graph_depth 等内部参数）

```json
search_context:
  { "query":        "string, required",
    "project_root": "string, required（绝对路径，正斜杠）" }
ask_project:
  { "question":     "string, required",
    "project_root": "string, required" }

> **TASK-MCP-BUDGET（2026-09-16）**：删除两个工具上的 `max_tokens`。
> 包大小是**服务端按证据密度决定的内部量**，不是调用方该猜的参数——实测 AI 会传偏小值
> （8000/10000）把长函数截断成"签名 + 前 15 行"，恰好毁掉它自己要的链路完整性。
> 运行时仍接受该字段（旧编辑器兼容），只是不再出现在 `tools/list` 的 `inputSchema` 中。
> 内部预算：Fast 14K / Deep 16K（见 Module/03 §4.1）。
```

工具 description 写法（notace/四项目共识——description 即行为控制）：包含 good/bad query 示例、与 grep/read 的分工边界（"精确标识符全量引用请用 grep；已知文件请直接 read"）、两工具间的导航（"需要直接结论用 ask_project"）。

> **TASK-MCP-BUDGET（2026-09-16）：description 以 `docs/contracts/mcp-tools.json` 为单一来源。**
> 此前 client 与 service **各写一份**，实测长度差 4 倍（client 298 字符 / service 1309 字符），
> 而 **AI 在 stdio 面看到的是 client 那份**（`protocol.rs` 的 `tools/list` → `tools::definitions()`）
> ——于是本节要求的"查询写法/分工边界/导航"实际到不了 AI 眼前。现在两端同源：
> client 用 `include_str!` 编译时嵌入契约、service 运行时读同一个文件，
> 两端各有逐字一致性测试守住（client `descriptions_come_from_the_cf06_contract`、
> service `test_tools_list_matches_cf06_field_by_field`）。
>
> 篇幅纪律：contract 版本控制在 ~400 字符/工具——描述过长会降低 LLM 的遵守概率，
> 只保留"分工边界 + 查询写法 + 调用链局限"三类高价值信息，返回格式与降级细节删去
>（AI 看到结果自然能懂）。

### 2.2 错误三分类映射【已验证：notace 模式，Background/01 §4】

| 类别 | 例 | MCP 表现 |
|---|---|---|
| 参数错误 | query 为空、project_root 含反斜杠 | JSON-RPC error -32602 |
| 工具执行错误 | 同步失败、检索超时、证据不足短路 | 正常响应 + isError:true + 人类可读原因（agent 可决策重试） |
| 协议错误 | 未知方法、非法 JSON | -32601 / -32700 / -32600 |

纪律：stdout 只出 JSON-RPC 帧；诊断/日志全走 stderr；所有对外错误先过 token 脱敏 + body 截断（512 字节）。

### 2.3 SDK 选型【权衡】

官方 MCP SDK（TS: @modelcontextprotocol/sdk；Rust: rmcp）而非 notace 式手写 500 行——手写的理由（早期 SDK 不成熟）已消失，协议版本跟进（2025-11-25 / 2026-07-28）交给上游。开放问题见 §9-1。

## 3. 同步客户端

### 3.1 忽略规则三层（D-28，补齐 notace 最大缺陷）

```text
优先级：.novaignore（项目内用户自定义）> .gitignore（真实解析，含否定规则）
        > 内置默认（.git node_modules target dist build .venv __pycache__ .tox
                    .idea .vscode .nova blobs 缓存目录等）
通用过滤：>128KB 跳过；>10% 不可打印字符判二进制跳过（notace 参数沿用）
Rust 实现直接用 ignore crate（原生支持 .gitignore 语义）
```

### 3.2 本地缓存与 verified cache hit【已验证：notace 模式】

```text
.nova/index.json：
  { "version": 1, "configHash": "...",          // 同步配置指纹，变更即失效
    "projectId": "...", "checkpointId": "...",
    "files": { "src/a.cpp": { "mtime", "size", "contentHash", "blobHashes": [...] } } }
```

- mtime+size 是快路径，**最终以 content hash 为准**（`contentHash = sha256(file_content)`；防 touch 误判与 mtime 不变内容变）
- `blobHashes` 使用 Module/01 定义的 `blob_hash`，用于源码镜像、上传幂等和 checkpoint scope
- 原子写（tmp+rename，Windows 先删）
- 上传被服务端接受的文件才入缓存（部分接受=不入，下次重传）

### 3.3 同步协议（时序）

```text
常规 tool call：
  ① scan（walkdir + 三层 ignore）
  ② 与缓存对账 → added / modified / deleted 集合
  ③ deleted → 通知服务端（01 级联删索引 + spec_references 置 stale）
  ④ added/modified → POST /batch-upload（每批 ≤1MB，附 branch/commit 元数据）
       服务端返回 { accepted: [blobHash], skipped: [path] }
  ⑤ scope 变化 → 重建 checkpoint（POST /checkpoint-blobs → checkpoint_id）
  ⑥ 检索请求带 checkpoint_id（而非全量 blob 名单——38k blobs ≈2.5MB 的教训）
```

**checkpoint 自愈**【已验证：notace 三条路径】：invalid checkpoint → 作废重传全量 scope 一次并重建；stale blobs（服务端报 unknown）→ 本地 forget → 重新同步 → 重试一次；scope_changed → 直接重建。每条恢复路径**只执行一次**，再失败才把错误交给 agent。

### 3.4 project identity 解析（D-29，外部建议未覆盖的空白）

多台机器查同一个 repo 应命中同一 project（共享索引），同一机器不同 checkout 应隔离：

```text
resolve_project(root):
  有 git remote → identityKey = sha256(remoteUrl + repo 相对路径)
  无 git        → identityKey = sha256(canonical 绝对路径)
  POST /resolve-project { identityKey, displayName } → { projectId, created }
（首次创建；remote URL 只存于用户自己的服务端，无第三方）
```

### 3.5 首同步与断点续传（D-31，对 notace 的关键改良）

```text
首次 tool call 触发全量同步：
  超过 120s 或单次上传限额 → 返回 index_in_progress（isError:true）
    { "progress": "63% (2.5w/4w files)",
      "hint": "后台继续同步，约 N 分钟；稍后重试本查询" }
  客户端后台线程继续分批上传（blob 已传即断点，天然续传）
下次 tool call：增量对账后正常检索
```

notace 的 180s 一把梭在十万文件级 C++ monorepo 会翻车（Background/01 §5 的超时注释自证）；nova 把"首次体验"从阻塞改为**可重试的进度反馈**。

### 3.6 freshness 语义（D-30：上传完成 ≠ 索引完成）

```text
同步完成（blob 全部 accepted）→ 服务端索引 job 异步跑（01 §4.3）
检索时不阻塞等待索引：
  ContextPack.freshness 如实报告 indexingFiles（03 合同字段）
例外：project 索引为空（首次）→ 检索直接返回 index_in_progress 重试提示
```

## 4. 分层超时矩阵（D-32）

| 操作 | 客户端→服务端 | 说明 |
|---|---|---|
| batch-upload（每批） | 30s | ≤1MB/批，notace 实测参数 |
| resolve-project / checkpoint | 10s | 轻请求 |
| search_context（整体） | 15s | 检索 ~0.5s（02 §5），15s 留同步余量 |
| ask_project（整体） | 90s | 60s LLM 上限 + 二轮检索 + 缓冲 |
| 首次全量 | 120s 后转后台 | §3.5 |

超时错误统一为可重试语义（isError:true + "重试本查询"提示），token 脱敏后返回。

## 5. 两个工具的完整链路（Fast 与 Deep 共用同步/检索/组装，不分叉）

```text
search_context：
  MCP → 同步（§3）→ POST /search { projectId, checkpoint, query, maxTokens }
     → 服务端：02 检索 + 03 组装 → 返回 ContextPack → 客户端渲染 Markdown → MCP 响应
ask_project：
  MCP → 同步 → POST /ask { projectId, checkpoint, question }
     → 服务端：02 检索（含 G 表二轮）+ 03 组装 → answerable?
          ├─ false → 03 短路包（D-24）
          └─ true  → 04 LLM + citation 回验 → answer + ContextPack 摘要
```

渲染位置【权衡】：ContextPack 的 Markdown 渲染放**服务端**返回（而非客户端）——渲染规则与合同强耦合，跟引擎同版本演进，客户端永远只做透传+协议。

## 6. 客户端形态与 CLI

- Rust 单二进制（notace 同款技术选型），npm platform-packages 分发（npx nova 即用）
- CLI 子命令：`nova login`（配置 server_url+token → ~/.config/nova/config.json）、`nova sync`（手动，debug 用）、`nova status`（同步/缓存状态）、`nova mcp`（输出各 harness 的 MCP 配置 JSON）

## 7. 安全要点（本组件范围内）

token 只存本地 config + 内存；不出现在任何日志/错误/URL；stdout 永远无 secret；HTTPS 强制（base_url 校验同 notace）；错误 body 截断脱敏。

## 8. V1 明确不做

| 不做 | 理由 | 何时 |
|---|---|---|
| 常驻 watcher / 文件监听 | 懒同步已覆盖（S2）；WSL2 inotify 不可靠（codegraph 教训，Background/02 §7） | V2 按平台评估 |
| 底层检索工具暴露（bm25_search 等） | D-12：Planner 工作不交还主 Agent | — |
| 客户端 AST/切片 | S3：客户端薄，切片策略服务端可升级 | — |
| 上传 gzip 压缩 | 先验证裸 JSON 批量够用 | 容量/带宽吃紧时 |
| 并发多项目同时同步 | 单 agent 会话串行足够 | 多根 workspace 场景出现时 |
| scope 差量协议（只传 hash 差集） | checkpoint 已解决重复上传；差集复杂度高 | 观察流量数据 |

## 9. 开放问题

1. **MCP SDK 语言**：客户端定为 Rust 二进制 → rmcp（官方 Rust SDK）成熟度待验证；不成熟则回退 notace 式手写（500 行内，协议面小）。
2. **batch-upload 是否需要 gzip**：文本源码压缩比 ~4x，大仓库首同步带宽减负——实现成本低，但 V1 先测裸传输耗时再定。
3. **deleted 通知的幂等语义**：服务端重复收到同一删除通知（重试导致）需幂等处理——01 的级联删除按 (project, path) 幂等设计即可，需在 API 合同中明确。
4. **多 harness 并发同一 project_root**：两个编辑器同时挂 nova MCP → 两个客户端进程各自有缓存/后台线程——V1 接受（最终一致），V2 考虑文件锁。

## 10. 与外部建议稿（本文覆盖前版本）的对照

| 其建议 | 本文处理 |
|---|---|
| 产品边界三层图 | **采纳**（§0） |
| V1 两工具、不暴露底层工具 | **采纳**（D-12，schema 正式化 §2.1） |
| notace 借鉴清单（blob/cache/checkpoint/自愈/错误映射） |  |
| tool call 自动同步（懒同步） | **采纳**（D-27，S2） |
| Source 同步与 Retrieval Chunk 分离 | **采纳**（S3，即 D-01/D-02 的客户端侧表述） |
| 工具 schema 简单化 | **采纳**（§2.1） |
| 其未覆盖，本文补充 | ① project identity 解析（§3.4，跨机器共享索引的关键）；② .gitignore 真实解析三层规则（§3.1，notace 最大缺陷的补齐）；③ freshness 语义：上传≠索引（§3.6）；④ 首同步断点续传+进度反馈（§3.5，对 notace 180s 阻塞的改良）；⑤ 分层超时矩阵（§4）；⑥ 渲染位置在服务端的权衡（§5） |
| 立场差异 | 其建议本地缓存 `.nova/index.json` 放项目内——**采纳但补两条**：缓存目录必须进内置 ignore（防自食）；config 放 `~/.config/nova/` 与项目内缓存分离（token 不落项目目录） |

## 11. 决策登记（已同步 INDEX.md）

D-27 懒同步 / D-28 ignore 三层 / D-29 project identity / D-30 freshness 语义 / D-31 首同步断点续传 / D-32 分层超时矩阵。
