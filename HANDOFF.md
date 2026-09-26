# zace 项目交接说明（2026-09-26 更新）

> **给接手 AI 的第一份文档**。读完这一份，你就知道：项目是什么、做到哪了、下一步做什么、别踩哪些坑。
> 详细任务清单在 `docs/tasks/README.md`；协作流程在 `docs/plan/orchestration.md` 与 `docs/plan/multi-ai-worktrees.md`。
> **跑基准前先读 [`benches/README.md`](benches/README.md)「新会话从这里开始」**：三靶场（`leveldb`/`HelloAgents`/`langchain`）的索引已持久化在 `/root/.zace/bench/voyage-4-lite-d1024`，**复用即可、不要再 ingest**；
> 设备绑定纪律与报告索引见 `benches/results/README.md`。
>
> **2026-09-26 快照（先看这段）**：
> - `main @ 3953d9a` ｜ ruff ✅ ｜ 依赖方向 ✅ ｜ **1340 passed, 9 skipped**（core 771 / service 552 / root 17）｜ CI（python + web）✅
> - 本轮完成**冷启动优化三连**：TASK-114（索引冷启动，langchain 真实 API 131.5s→77.6s）、
>   TASK-115（向量阶段多窗口并行，79.5s→**63.3s**）、TASK-116（硬件配置档案）。
>   报告：[`index-perf-task114-vps.md`](benches/results/index-perf-task114-vps.md)、
>   [`index-perf-task115-vps.md`](benches/results/index-perf-task115-vps.md)；调优档：[`configs/profiles/`](configs/profiles/README.md)。
> - 当前 langchain 冷启动账（K=2 / 真实 API / 63.3s）＝ **网络窗口 34.0s + 消费者本地 ~17s + 尾窗/GIL ~12s**；
>   生产者本地 24.5s 基本被网络盖住。**硬地板是 API 配额**：3.56M token ÷ 16M TPM ≈ **13.4s**。
> - 质量护栏（各自新建索引的 A/B）：leveldb recall@5 **1.000**、HelloAgents **0.842**；负例 1/1 通过。
> - 活跃卡只剩 **TASK-109 / TASK-093 / TASK-023**（见 §3）；运维侧 3 件小事也在 §3。
>
> **留给架构优化的已知入口（2026-09-26 重排）**：
> ① 检索的**符号级定位偏弱**——`.h/.cc` 与同主题 doc 互串（qa-quality §4-1）→ TASK-109；
> ② **负例可回答性阈值偏宽**——名字沾边即判 `answerable`（§4-4）；
> ③ 冷启动再压缩只剩两条路：**减少 token 总量**（chunk 策略，有质量代价）或**升 TPM 配额**；
>    想把"消费者本地那 ~5s"也藏进网络，需要动 provider 内部（取回与解码分离）；
> ④ **维度不可配**——API 模式未透传 `output_dimension`（`baseline-v1.md` §5-1）；
> ⑤ **chunk 参数无 env 入口**，且改动触发 `full_reparse`（必须先换数据根，§5-2）。
>
> **§1–§2、§4–§7 是 2026-09-14/15 的历史记述**（Phase 1–4 的完成情况、工具返回形态、约束与环境事实），保留作背景；
> 与它们冲突时，以本快照、§3/§8 与 `docs/tasks/README.md` 为准。

---

## 0. 一句话现状

**整条链路已上线可用**：网页注册/登录（邀请码 + 身份分级）→ 创建 API Key → `npx zace-client` 接入 Codex
→ 真实提问 → 返回带「文件:行号」的证据。检索、服务化（REST + MCP）、Rust 客户端、WebUI、鉴权/租户/统计/
管理员后台全部可用且 CI 绿。**当前重心是性能与质量打磨**：冷启动已完成两轮优化（§3），质量侧等真实使用
数据（TASK-093）驱动。

---

## 1. 项目是什么

zace = 给 Coding Agent（Codex/Cursor/Claude Code/pi）提供**代码库上下文检索**的引擎。
Agent 通过 MCP 调 `search_context` / `ask_project`，拿到**带「文件:行号」证据**的上下文包。

**四个物理单元**（monorepo，依赖方向 `web, client → service → core`，CI 强制检查）：

| 目录 | 单元 | 职责 |
|---|---|---|
| `core/` | zace-core | 引擎纯库：切片/存储/四路召回/RRF/图扩展/装填/渲染。**零 HTTP、零用户概念** |
| `service/` | zace-service | 服务化外壳：FastAPI REST + MCP 端点 `/mcp` + 鉴权 + 租户 + 索引/审计 |
| `client/` | zace-client | 本地 MCP stdio 客户端（Rust）：扫描/哈希/上传 |
| `web/` | zace-web | SPA 管理面（React/Vite/TS） |

技术栈：Python 3.12（uv）+ Rust + React/Vite。

---

## 2. 已完成（均已合并 main 并独立验证）

### Phase 1–2：core 检索闭环 + service 外壳

| 项 | 状态 |
|---|---|
| core：切片/存储/FTS5/四路召回/RRF/图扩展/装填/渲染/CLI/eval | ✅ |
| service：REST（查询/同步/项目）+ MCP 端点 `/mcp`（Streamable HTTP） | ✅ |
| 本地单用户模式：`zace-service local --repo <路径>` 一键起 + 后台索引 + 懒重扫 | ✅ |
| embedding 架构：按模型配置化 + provider 解耦 + 并发 | ✅ |

### Phase 3–4：鉴权 + 租户 + 统计 + WebUI（最近一波，A–H）

| 卡 | 内容 | 验证证据 |
|---|---|---|
| TASK-060 | 鉴权（session + API token + bootstrap + metadb） | 实测 bootstrap 201 / token 鉴权 200 |
| TASK-061 | **租户双层**（token → user → owns project） | 实测双用户越权：10 端点全 404，无凭据 401 |
| TASK-062/085 | 索引 job 落库与统计 | 实测 `index-stats` 0 → **1** |
| TASK-064/084 | 查询审计与用量 | 实测 `usage/summary` 0 → **3** |
| TASK-070 | WebUI（登录/控制台/API Key/历史/接入指南） | 746→804 pytest + 38 vitest + build 全绿 |
| TASK-080 | 接入指南两卡牌（npm 下载 + Agent 配置带 `--token`） | 实测生成的配置可直接用 |
| TASK-081 | 密码下限 8 → 3 | 真服务 3 位密码 bootstrap 201 |
| TASK-082 | 删除 Playground 与项目管理页 | 死链 grep 0 命中 |
| TASK-083 | 空态/错误态统一 | `catch{return[]}` → 抛错，不再把故障显示成空数据 |
| TASK-086 | 导航重排（控制台/接入指南/API Key/历史）+ 网格背景 | 截图核验 |

### Phase 3 补强（**2026-09-14 晚，四卡已合并并端到端验证**）

| 卡 | 内容 | 实测证据（编排者亲测） |
|---|---|---|
| TASK-087 | `next_queries` 渲染进正文（`### Suggested Next Queries` 节）+ `answerable=false` 短路包 | 三个无关问题均返回 `insufficient_evidence` + 完整短路包 |
| TASK-088 | `ask_project` 接入 LLM（可配置 `ANSWER_*` + Grounded Prompt + Citation 回验 + 设置页） | **真实 LLM 回答 2804 字，citation_coverage=1.0，879ms**；改 env 即改模型已验证 |
| TASK-089 | MCP 面归属校验（上云前最后越权口已堵） | bob 越权两工具均 `project_not_found`，无探测面 |
| TASK-090 | 请求日志持久化（文件轮转 + 有界窗口）+ trace id 查询 | 失败请求 → 拿 `X-Request-Id` → 查到完整日志（路径/状态/耗时/用户/错误码） |

**合并后的 `ask` 分支顺序**（087 + 088 合并后的真实语义，**证据优先**）：

| 情况 | `status` | 返回 |
|---|---|---|
| `answerable=false` | `insufficient_evidence` | 短路包（不调 LLM，含 `bestEffortContext`/`missingEvidence`/`nextQueries`） |
| 未配置 `ANSWER_*` | `degraded` | 前置说明 ＋ 渲染包 |
| 已配置但调用失败/超时 | `degraded` | 故障说明 ＋ 渲染包 |
| 成功 | `answered` | LLM 答案（已回验引用） |

### 真实端到端验证（编排者亲自跑的，不是采信报告）

```
Codex 调 search_context → 898 文件... 实际 236 文件 / 2729 chunks 索引
Voyage 嵌入：POST https://api.voyageai.com/v1/embeddings "200 OK"
返回证据：hello_agents/core/streaming.py:74-82 等带行号的代码块
审计落库：query_audit 1 条（answerable=1, confidence=medium, latency_ms=322）
```

---

## 3. 下一步做什么（2026-09-26）

### 活跃卡（只有这三张）

| 卡 | 标题 | 硬依赖 | 状态 |
|---|---|---|---|
| [TASK-109](docs/tasks/TASK-109-EvidenceGap二轮补检.md) | Evidence-Gap 二轮补检（检索质量，用户点名"下一轮重点"） | TASK-108 ✅ | **pending** |
| [TASK-093](docs/tasks/TASK-093-真实数据闭环.md) | 真实使用数据采集闭环（**不含调参**） | TASK-084 ✅ / TASK-091 ✅ | **pending** |
| [TASK-023](docs/tasks/TASK-023-真实场景用例采集.md) | 真实场景用例采集（由 TASK-093 落地，不单独开工） | TASK-040 ✅ | **pending** |

**推荐顺序**：TASK-109（搜得不够全）→ TASK-093（量不准）→ TASK-023 随 093 回填。
质量参数（R29/R30）**仍未解冻**，需用户单独授权——TASK-050 暂不开卡。

### 本轮性能工作（已完成并合并，2026-09-26）

| 卡 | 内容 | 实测结果 |
|---|---|---|
| TASK-114 | 索引冷启动：ASCII 快速路径（跳过 jieba）、向量流式 sink、冷启动 SQL 快路径、lazy `lancedb`、本地/网络流水线 | langchain 真实 API **131.5s → 77.6s**；峰值 RSS 1143→697MB；`import zace_core.cli` 4.2s→**0.66s** |
| TASK-115 | 向量阶段多窗口并行（消费者 K=2，**整窗派发**） | langchain **79.5s → 63.3s（-20%）**、HelloAgents 17.2→16.0s；峰值 +75MB |
| TASK-116 | 硬件配置档案 `configs/profiles/<机器标识>.env` | 现役 `154.12.34.214` 已冻结留档；新机 `dmit-2c2g-200m` 占位档待实测 |

证据：[`index-perf-task114-vps.md`](benches/results/index-perf-task114-vps.md)、
[`index-perf-task115-vps.md`](benches/results/index-perf-task115-vps.md)（后者 §2 留了
"窗口不能按消费者切小"的负结果）；原始 JSON 在 `benches/results/raw/task114/`、`raw/task115/`。

### 运维侧待办（需要用户动手，AI 改不到）

1. `/etc/zace/zace.env` 显式加 `ZACE_EMBED_WORKERS=2`（不写也是 2，写出来便于留档与回滚）；
2. systemd `MemoryHigh=800M` 建议抬到 **1000M**（TASK-115 后 langchain 规模的服务端峰值实测 823MB），
   `MemoryMax=1400M` 不动；
3. 换新机（2C/2G/200Mbps）后：**4 流并行实测出口带宽** → 跑探针 → 回填
   `configs/profiles/dmit-2c2g-200m.env` 并把状态改成 ✅（该文件尾部有"必做三件事"）。

### 用户的人工任务（非 AI 卡）

1. 打磨两个工具的返回内容；2. 补真实问题 + 打磨 benchmark（配合 093）；
3. 质量参数解冻需单独授权（TASK-050 暂不开卡）。

---

## 4. 两个工具的返回（接手前必须理解）

### `search_context` → 渲染好的 ContextPack Markdown

```markdown
## Relevant Context
### Code
[E3] StreamBuffer — hello_agents/core/streaming.py:74-82
     reason: bm25 -11.46 + bm25 rank 32 + vector 0.4632 + vector rank 3 + entry point +0.2
     74 | class StreamBuffer:
### Docs
### Missing Evidence
- [unresolved_reference] 70 个符号引用无法解析...
### Meta
confidence: medium | index: fresh | budget: 5.6K/6.0K
```

**三条特色**：`reason:` 行标明每条证据靠什么召回；`[E*]` 编号是引用回验的基础；
`Missing Evidence` 诚实报缺口（不假装找到了）。

### `ask_project` → **已接 LLM（TASK-088，2026-09-14 合并）**

```python
# service/zace_service/routers/query.py（四分支，证据优先）
if not pack.answerable:            return _insufficient_package(...)   # D-24 不调 LLM
if provider is None:               return _degraded_response(...)      # 未配置
# 调用失败/超时 → _degraded_response；成功 ↓
return {"status": "answered", "answer": outcome.answer, ...}
```

配置全部走环境变量（`.env` 里已配好）：`ANSWER_BASE_URL` / `ANSWER_API_KEY` / `ANSWER_MODEL`，
另有内置默认值 `ANSWER_TIMEOUT_S=60` / `ANSWER_MAX_TOKENS=3072` / `ANSWER_TEMPERATURE=0.2`。
设置页（`/settings`）展示 embedding 与 LLM 的当前生效配置，**不泄露 key 任何片段**。

**实测**（编排者亲跑，对 zace 自身索引）：问「next_queries 如何渲染？」→
`status=answered`、2804 字回答、带 `[E6]`/`[E13]` 等引用、`citation_coverage=1.0`、`latency_ms=879`。

~~**TASK-088 就是把它接上**~~ —— **已完成（2026-09-14）**：设计定稿在
`docs/design/Module/04-AI总结.md`，配置项 `ANSWER_BASE_URL/API_KEY/MODEL` 全走环境变量，
设置页（`/settings`）展示当前生效配置。

### 架构浪费的修复现状（TASK-087/088 已落地）

| ContextPack 产出 | Agent 能看到 |
|---|---|
| `evidence[]`/`docs[]`/`missing_evidence[]`/`confidence` | ✅ |
| `next_queries[]` | ✅ **已渲染**（`### Suggested Next Queries` 节，TASK-087） |
| `answerable` | ✅ **已用于分支**（false → 短路包，不调 LLM） |
| `flows[]`（调用链） | ⚠️ 需图扩展命中，实测常为空（未改善） |

---

## 5. 关键约束（别踩）

### 质量参数冻结（R29/R30）

`docs_ratio=0.10`、`CONSENSUS_SCORE_RATIO=2.15`、`rerank` 权重等是 **smoke 集上的拟合值**，
**未经真实数据校准**。**不得基于现有测试集调参**；真实优化要等 TASK-093 的数据。

### 契约变更走 L1/L2/L3 流程

- **实施 AI 永不直接改** `docs/contracts/**`、`docs/design/**`、`zace_core/{types,interfaces,hashing}.py`；
- 需要新增字段/改签名 → 在任务卡"执行记录"写**契约变更申请**，停下来等编排者；
- **CF-05**（REST 路径与错误信封）、**CF-06**（MCP 工具 schema）是冻结合同。

### 一个工作区一个会话

**一个 AI 会话 = 一个独占 worktree = 一个分支**。
在**主工作区**（`/root/xuwenzheng/ace/zace`）改代码是禁止的（只用于集成）。

```bash
cd /root/xuwenzheng/ace/zace
bash scripts/lane-worktrees.sh status              # 看哪个 lane 空闲（lane 目录可能已失效，以 git worktree list 为准）
# 本机可直接开临时 worktree（本轮实践）：
git worktree add -b feature/task-xxx_<缩写><MMDD> /root/xuwenzheng/ace/zace-<用途> main
# 干完：基线三条绿 → 回填卡片 → commit →（默认不 push；本仓当前由用户授权 AI 直接推 main）→
#      git worktree remove /root/xuwenzheng/ace/zace-<用途> && git branch -d feature/task-xxx_<缩写><MMDD>
```

**教训**（真实发生过）：多个会话共用一个目录时，`git commit` 提交到哪个分支取决于
"谁最后切了 HEAD"，曾导致提交错位、工作被卷进别人的提交。**务必用独立 worktree。**

### lane worktree 可能是旧提交

新建的 lane 处于 `detached HEAD`，可能**落后于 main 很多**（实测出现过停在 21 个提交之前、
连任务卡文件都不存在的 lane）。**开工前先 `git log --oneline -1` 核对**，不对就从 `main` 开分支。

### 基线三条（每次提交前必须绿）

```bash
uv run ruff check .
uv run python scripts/check_dependency_direction.py
uv run pytest -o addopts="" -q      # 注意：不加 -o addopts="" 看不到汇总行
```

前端：`cd web && npm run lint && npm test && npm run build`

---

## 6. 当前环境的事实（2026-09-14 核实）

| 项 | 值 |
|---|---|
| **仓库根** | `/root/xuwenzheng/ace/zace`（**2026-09-26 复核**；历史文档里的 `~/github/ACE/zace`、`/home/...` 路径在本机不存在） |
| **泳道工作区** | `/root/xuwenzheng/ace/zace-lane-{a..j}`——**注意**目录里的 `.git` 可能指向已消失的旧路径（以 `git worktree list` 为准）。本轮实践：临时 `git worktree add -b <branch> /root/xuwenzheng/ace/zace-<用途> main`，收工 `git worktree remove` + `git branch -d` |
| Python | 3.12（uv 管理；`uv sync --all-packages --all-extras` —— 只 `uv sync --frozen` 不会装 pytest/ruff） |
| Node / npm | v24.15.0 / 11.12.1（**CI 用 Node 22**；本机 Node 24 跑 web 套件会出现 jsdom/undici 的 `AbortSignal` 报错，别拿它当 web 判据） |
| **配置分层** | `/etc/zace/zace.env`（0600，含真密钥，不进 Git）→ `configs/profiles/<机器标识>.env`（非密钥调优档，进 Git；见 §3 与 `configs/profiles/README.md`） |
| **cargo** | ✅ **1.97.1 可用** —— Rust client 可本地构建（`cd client && cargo build --release`，实测 1m57s） |
| **docker** | ✅ **29.1.3 可用，daemon 在跑** —— TASK-092 可本地部分验证 |
| **Playwright** | ✅ chromium 已装（`~/.cache/ms-playwright/chromium-1243`）；`--with-deps` 需 sudo |
| **http_proxy** | ⚠️ 已设（`http://127.0.0.1:7890`）—— **连本机服务必须 `NO_PROXY=127.0.0.1,localhost`**（`.env` 里已配） |
| **embedding key** | ✅ `.env`（仓库根与每个 lane 都有，**已被 gitignore**）：Voyage `voyage-4-lite` 主路径 + 硅基流动 `bge-m3` 备选；两者实测 200 |
| **LLM key** | ✅ `.env` 的 `ANSWER_*`（xiugou / deepseek-v4.1-flash），实测 200 |
| **基准靶场** | `/root/xuwenzheng/ace/benchmark/{leveldb,HelloAgents,langchain}`（**只读**！不要在里面建文件；旧的 `Agent开发/hello-agents` 靶场已不用） |
| **密钥文件** | `/etc/zace/zace.env`（0600，生产/基准都用它；仓库根**没有** `.env`）。用法：`set -a; source /etc/zace/zace.env; set +a` |
| 磁盘 | 820G 可用 |

**`.env` 用法**（每个工位已铺好）：

```bash
set -a; source .env; set +a     # Voyage/LLM key + NO_PROXY 一次到位
```

> **注**：`/etc/zace/zace.env` 含真实 key，**永不提交**（也不在仓库里）。非密钥的调优档见
> `configs/profiles/<机器标识>.env`（进 Git）。

---

## 7. 本地怎么跑起来（2 分钟）

```bash
cd /root/xuwenzheng/ace/zace
set -a; source /etc/zace/zace.env; set +a      # Voyage/LLM key + NO_PROXY
set -a; source configs/profiles/154.12.34.214.env; set +a   # 本机调优档（可选）

# 后端（云端形态，可看到登录/API Key/统计）
ZACE_LOCAL_MODE=false ZACE_REGISTER_OPEN=true ZACE_DATA_ROOT=/tmp/zace-dev \
  uv run zace-service serve --host 0.0.0.0 --port 8896

# 前端（另一个终端）
cd web && ZACE_WEB_API=http://127.0.0.1:8896 npx vite --port 5174 --host 0.0.0.0
# 浏览器打开 http://localhost:5174/
```

> **端口选择注意**：WSL 会误报某些高位端口"address already in use"（实测 8898/8901/18897）。
> 建议直接用 **18898/18896** 这类实测可用的端口，或先 `ss -ltn` 确认。

**Agent 接入用的地址是后端**（`http://localhost:8896`），**不是**前端端口（5174 只服务浏览器请求）。

```toml
# ~/.codex/config.toml
[mcp_servers.zace]
command = "npx"
args = ["zace-client", "--base-url", "http://localhost:8896", "--token", "zace_你的Key"]
startup_timeout_ms = 60000
```

> **首次提问会等一会儿**（几分钟量级，取决于仓库大小）：客户端会先扫描本地仓库、
> 增量上传、服务端对文件做 embedding，然后才检索。**第二次起只传改动文件，通常 1-3 秒。**

---

## 8. 已知缺口与风险

| # | 缺口 | 严重度 | 处置 |
|---|---|---|---|
| 1 | ~~MCP 面仍未做归属校验~~ **已修复**（TASK-089 已合并，实测 bob 越权两工具均拦） | ✅ | done |
| 2 | `/healthz` 免鉴权且**列出全部 projectId/项目进度**（枚举面） | 🟡 | **仍未处理**（2026-09-26 复核：`ops.py` 的 `_project_progress` 照旧返回 `projects`，且路由无鉴权依赖） |
| 3 | ~~日志只在 stdout，无持久化~~ **已修复**（TASK-090，文件轮转 + trace id 查询） | ✅ | done |
| 4 | ~~`ask_project` 未接 LLM~~ **已修复**（TASK-088，实测 citation_coverage=1.0） | ✅ | done |
| 5 | ~~`next_queries` 已生成但不渲染~~ **已修复**（TASK-087） | ✅ | done |
| 5b | ~~无任何存储配额机制~~ **已实现**（`metadb.py` / `routers/admin.py` 已有配额判定与展示，TASK-094 已合并） | ✅ | done |
| 6 | 无 CI 覆盖的 e2e（需起服务造数据） | 🟢 | 未开卡 |
| 7 | 历史页逐项目拉明细（N 次请求） | 🟢 | 未开卡 |
| 8 | 参数冻结未解（质量优化受阻） | 🟡 | TASK-093 数据充分后由用户授权 |
| 9 | 多人共用同一仓库 → 只有第一个认领者能用（V1 简化） | 🟡 | 设计已记录（`org_id` 留 V2） |
| 10 | 无域名时自动 TLS 不可用 → key 明文暴露风险 | 🟡 | TASK-092 **已部署**（nginx + 静态产物，见 `docs/handbook/deployment/vps.md`）；TLS 现状按该文复核 |
| 11 | 未认证请求（401）的日志无 owner → 用户报错时若只给未认证请求的 id，管理员查不到 | 🟡 | TASK-090 登记为未决（V1 无角色体系） |
| 12 | `docs/contracts/openapi.yaml` 第 167 行有 YAML 语法瑕疵（未闭合引号）——**既有问题，非本轮引入**；项目用按缩进解析故不影响测试 | 🟢 | 未开卡 |
| 13 | 服务端 systemd `MemoryHigh=800M`：TASK-115 后 langchain 规模的服务端峰值实测 **823MB**，会持续触发内存回收 | 🟡 | **建议抬到 1000M**（`MemoryMax=1400M` 不动）——运维动作，见 §3 |
| 14 | `/etc/zace/zace.env` 未显式写 `ZACE_EMBED_WORKERS`（走代码默认 2） | 🟢 | 不影响行为，但建议显式留档（便于回滚/对照）——见 §3 |
| 15 | 新机档案 `configs/profiles/dmit-2c2g-200m.env` 是**外推值**，未实测 | 🟡 | 上机后按该文件尾部"必做三件事"跑探针并回填状态 |

---

## 9. 协作纪律速查

| 规则 | 说明 |
|---|---|
| 一张卡一个会话 | 不并行做多卡；不顺手重构其它模块 |
| 只管清单内文件 | 任务卡的"交付物所有权"是硬边界；改公共文件先申请 |
| 推送 | 泳道模式下**只需本地提交**；本仓当前由用户授权 AI 直接 `git push origin main`（禁止 `--force`、禁止移动已有 tag）。合并/评审由编排者（用户）拍板 |
| 完成即回填 | 卡片"执行记录" + `docs/tasks/README.md` 对应行改 `review` |
| 有疑问就停 | 契约/设计冲突、需求不明 → 写进"未决问题"并停下，不要自行拍板 |
| 不夸大验证 | "跑过了"必须有真实输出；没跑的明确写"未验证" |

---

## 10. 交接时的当前现场（2026-09-26）

- **工作区**：主工作区 `/root/xuwenzheng/ace/zace` 在 `main @ 3953d9a`、干净；本轮的临时 worktree
  （`zace-perf` / `zace-par` / `zace-cfg`）与对应分支已删除，`git worktree list` 只剩主工作区；
- **运行中的服务**：`zace-service`（systemd，127.0.0.1:8899，配置 `/etc/zace/zace.env`）。
  ⚠️ **推 main 不会自动部署**：服务端跑的是部署时的代码，升级动作见 `docs/handbook/deployment/vps.md` §9；
- **临时数据根（可安全删除，不影响仓库）**：`/tmp/p0926-*`、`/tmp/t115b-*`、`/tmp/t116-*`（本轮真实 API 冷启动对照）、
  `/tmp/ha-branch`（换分支复用实验的靶场副本）、`/tmp/index-perf-handoff.main-untracked-backup.md`（合并前的未跟踪文件备份，内容已入库）；
- **未提交改动**：无；
- **靶场状态**：`benchmark/{leveldb,HelloAgents,langchain}`（只读）——`leveldb @ 7ee830d`、`HelloAgents @ 93e77ea`
  与 golden 登记一致；`langchain` 当前在 **`e75dae1f`**（比 `targets.json` 登记的 `41d3572` 新，做 golden 对照时必须注明）。

---

## 11. 下一步建议（给接手 AI）

1. **先读**：本文件（尤其顶部"2026-09-26 快照"）→ `docs/tasks/README.md` → `docs/plan/orchestration.md`（泳道模式、契约流程）；
2. **挑卡**：只剩 TASK-109 / TASK-093 两张，互相独立可并行（TASK-023 随 093 回填）；
3. **动性能参数前**：先读 `configs/profiles/README.md`（旋钮含义、内存实测、命名约定、"窗口不能按消费者切小"），
   改完把实测回填到对应档案；
4. **别踩**：`docs/design/**`、`docs/contracts/**`、`core/zace_core/{types,interfaces,hashing}.py` 是冻结面；
   本机（1.9G）整包 `pytest` 单进程可能被全局 OOM 杀（`exit=137`），按 `core/tests` / `service/tests` / `tests` 分三段跑；
5. **推送**：用户已授权 AI 直接 `git push origin main`；工作区隔离与合并纪律见 `docs/plan/multi-ai-worktrees.md`。
   —— 这些依赖 087/088/091 的产出，做完后需要用户参与评审。
