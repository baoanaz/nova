# 可优化项待办（Optimization Backlog）

> 建立：2026-10-04（基于 `main @ 10800ab` 的通读；只读代码 + 既有报告，**未跑新基准**）。
> 用途：给后续 AI 会话挑活。每项都写了**证据锚点 / 判断 / 方向 / 怎么验 / 边界**，挑中后按惯例开任务卡
> （`docs/tasks/TASK-TEMPLATE.md`），本文件只做索引，不替代任务卡。
> 维护：做完一项就在“状态”列写卡号 + 合并提交；发现判断错了就直接改本文件并注明日期。

## 0. 使用纪律

- **先测后改**：标了“需实测”的项，先用 profiler / trace 确认量级再开卡，不要按本文件的推断直接动手。
- **R29/R30 冻结仍有效**：凡是改变 rerank 权重、可回答性阈值、装填比例的项（标“⚠️ 需授权”），
  必须等用户单独授权（TASK-050 / TASK-093 的数据），不得在现有 golden 上拟合。
- **契约面**：标“契约”的项涉及 `docs/contracts/**`（含 `index-schema.sql`），须先在任务卡执行记录里提申请、等裁决。
- 质量回归口径：三靶场 `zace-core eval`（recall@5/@10、MRR、负例）**不允许下降**，见 `benches/README.md`。

## 1. 总览（按“收益 ÷ 成本”排序）

| # | 项 | 类别 | 成本 | 收益 | 前置 | 状态 |
|---|---|---|---|---|---|---|
| Q1 | 向量检索同分排序不确定 → 结果不可复现 | 质量/工程 | 极小 | 中 | 无 | 待办 |
| L1 | rerank/expand 的 N+1 SQLite 点查 | 延迟 | 小 | 中（需实测） | profile | 待办 |
| L2 | `reexport_sources` 每个 seed 全扫 imports 边 | 延迟 | 小 | 中（需实测） | profile | 待办 |
| S1 | 远端同步路径与基准路径不一致（逐批同步 ingest） | 冷启动/体验 | 中 | **高** | 实测对照 | 待办 |
| S2 | D-31“首同步 120s 转后台”未实现 | 体验 | 中 | 高 | S1 方案 | 待办 |
| Q2 | 显式路径只“标记”不“召回” | 质量 | 小 | 中 | 无 | 待办 |
| Q3 | 文件名/路径不参与 BM25，rerank 无路径特征（.h/.cc、doc/code 互串） | 质量 | 小~中 | 高 | ⚠️ 需授权 | 待办 |
| Q4 | `inferred` 单条命中即判可回答（负例偏宽） | 质量 | 小 | 中 | ⚠️ 需授权 | 待办 |
| S3 | LanceDB 从不 compaction / 清理旧版本 | 运维/延迟 | 小 | 中（长期） | 无 | 待办 |
| L3 | 召回通道串行：向量 embedding 网络往返不与 SQLite 通道重叠 | 延迟 | 小 | 小~中 | profile | 待办 |
| L4 | 本地 ONNX 首查询无 warm-up，必然超时降级 | 质量（本地形态） | 极小 | 小 | 无 | 待办 |
| L5 | `spec_references` 缺 `spec_block_id` 普通索引 | 延迟 | 小 | 小 | 契约 | 待办 |
| A1 | 索引与查询同进程（GIL / 内存互相拖累） | 架构 | 大 | 视用户量 | 触发条件 | 观察 |
| M1 | 超大文件拆分（assembly / engine / metadb） | 可维护性 | 中 | 中 | 随功能卡顺手 | 观察 |
| D1 | 设计文档与代码的 G3/G4 漂移 | 文档 | 极小 | 小 | 编排者 | 待办 |

---

## 2. 检索质量

### Q1 向量检索同分排序不确定

- **证据**：`core/zace_core/vectors/store.py:243` 只按 `score` 排序（`hits.sort(key=lambda hit: hit.score, reverse=True)`），
  同分时沿用 LanceDB 返回顺序；TASK-109 执行记录“未决问题 1”已实测：同一 query 连续调用约 7 个低分位次互换，
  `cockpit-0035` 的 `backfilled` 在 14/15 间浮动。
- **判断**：**值得做**。可复现性是所有 A/B 的地基；改动一行、零质量风险。
- **方向**：排序键改为 `(-score, chunk_id)`（与 `rerank` 的 tie-break 口径一致）。
- **验证**：同一 query 连跑 N 次，候选序列逐位相等；三靶场 eval 不回退。

### Q2 显式路径只“标记”不“召回”

- **证据**：`core/zace_core/retrieval/fusion.py:213` `_apply_explicit_paths` 只给**已被其他通道召回**的同路径候选升 tier 0；
  `merge` 的 docstring 明写“不做文件枚举”。即用户写了 `db/dbformat.h`，若 BM25/向量都没捞到该文件，它就不会进池。
- **判断**：**值得做**。“用户点名的文件不在结果里”是最难解释的失败；`chunks(file_path)` 已有索引
  （`schema.sql` 的 `idx_chunks_file`），成本低。
- **方向**：显式路径词元 → `Store` 新增按路径（含后缀匹配）列 chunk 的读 API → 作为 `exact` 通道候选进池
  （每路径限 N 块，优先 class/function，避免大文件刷屏）。不改 schema。
- **验证**：新增单测（点名未被召回的文件也能进池）；leveldb L-03/L-11 类用例观察。

### Q3 文件名/路径不参与匹配（.h/.cc、doc/code 互串）⚠️ 需授权

- **证据**：
  - `core/zace_core/storage/schema.sql:104`：FTS5 的 `file_path UNINDEXED` → 查询词命中文件名（`dbformat`、`log_format`）对 BM25 无贡献；
  - `retrieval/rerank.py` 的 17 条特征里没有“查询词 ↔ 文件名/路径段”相关项；
  - `benches/results/qa-quality-v1.md` §4-1：leveldb 4 条 search 失败全是同主题文件互串
    （`dbformat.h`→`version_set.h`、`doc/log_format.md`→`db/log_format.h`、`version_edit.cc`→`.h`）。
- **判断**：**这是当前检索质量最大的单点短板**（TASK-109 补的是“池内未进包”，不解决“排序选错同主题文件”）。
- **方向（由轻到重）**：
  1. rerank 新增特征“查询内容词命中文件 basename（去扩展名、按 `_`/`-`/驼峰切分）”——零重建；
  2. C/C++ 声明↔定义配对：`assembly._prefer_definition_representatives` 已在装填侧做代表选择，
     但图扩展侧没有 `.h ↔ .cc` 同 fqn 配对边；可在 `expand` 对 C/C++ seed 补“同 fqn 的另一侧”；
  3. 最后才考虑 FTS 增加 `path_seg` 列（**契约** + 全量重建）。
- **验证**：leveldb 4 条失败用例 + 三靶场整体不回退；新特征权重按 TASK-105 的离线回放方式定档。

### Q4 `inferred` 单条命中即判可回答 ⚠️ 需授权

- **证据**：`core/zace_core/contextpack/assembly.py:1351`：`answerable = explicit_hits >= 1 or inferred_hits >= 1 or ...`。
  `inferred` 来自正则抽取的驼峰/蛇形词（`retrieval/exact.py` `extract_inferred`），只要查询里有一个词恰好是仓库里某个符号名就判可答；
  `qa-quality-v1.md` §4-4：负例 2/3 被判 `answerable=True`。
- **判断**：方向成立，但**先用 trace 确认负例实际走的是哪条依据**（也可能是 `corroborated` 分支），再改。
- **方向**：`inferred` 作为硬依据时追加条件（如：命中符号的 chunk 最终进包且位于前 K，或同时有第二通道共识）；
  `explicit` 保持硬依据不变。
- **验证**：负例通过率上升，正例 `answerable` 不下降（三靶场 + cockpit）。

---

## 3. 查询延迟

> 背景数据：`benches/results/phase2-bakeoff.md` §3.1 查询 P50 0.68–2.27s，而纯 query embedding 仅 0.02–0.05s；
> TASK-109 执行记录写“端到端 ~600ms，受 embedding 主导”（API 模式）。两者口径/时期不同，**开卡前先在 langchain 上
> 对 `Engine.search_with_trace` 跑一次 cProfile**，按实测排 L1/L2/L3 的优先级。

### L1 rerank / expand 的 N+1 点查（需实测）

- **证据**：
  - `retrieval/rerank.py:536` `_exported_chunk_ids`：对**每个候选**（~120 + 扩展 30）调 `store.exact_symbols(fqn, limit=None)`；
    `:552` `_stale_spec_chunk_ids`、`:563` `_query_names_test_symbol` 同样逐个查；
  - `retrieval/expand.py:114` `_callee_rank`：对每个 seed 的**每条 callee 边**查一次 `exact_symbols`（`create_agent` 有 90 条 callee，seeds=20）；
  - `expand.py:135` `_calls_neighbours` 每个 seed 被调用两次（callees + callers），各自 `edges_for` 拉一遍双向边；`build_flows` 又重复走一遍；
  - 第二轮补检后 `collect_index_signals` 与 `assemble` 再跑一遍。
- **判断**：典型 N+1，单次查询可能上千次点查；SQLite 点查便宜但 Python 往返不便宜。**值得做，改动局部、无质量风险**。
- **方向**：① 单次查询内的 `exact_symbols` / `edges_for` 记忆化（请求级 dict，不跨请求，避免失效问题）；
  ② `_exported_chunk_ids` 改批量 `IN` 查询；③ `edges_for` 每 seed 只查一次后按方向分拣。
- **验证**：cProfile 前后对比 SQL 调用次数与墙钟；输出 ContextPack 逐字节一致（确定性管线，可直接 diff）。

### L2 `reexport_sources` 每个 seed 全扫 imports 边（需实测）

- **证据**：`storage/store.py:914`：`WHERE kind='imports' AND source LIKE '%__init__.py'`（前导通配，无法走索引）后在 Python 里按尾名过滤；
  由 `expand._expand_reexport_sites` 对**每个代码 seed** 调用（最多 20 次 / 查询）。langchain 这类 `__init__.py` 重导出密集的仓库最明显。
- **方向**：单次查询只扫一次，建 `尾名 → [source]` 映射复用；进一步可在 `Store` 实例上缓存（ingest 后失效）。
- **验证**：同 L1。

### L3 召回通道串行

- **证据**：`retrieval/__init__.py:150` `recall` 先依次跑 exact / literal / inferred / BM25，**之后**才进入向量通道（API 模式含一次网络往返）；
  模块 docstring 自述“先串行实现，接口按并行语义设计”。
- **方向**：进入 `recall` 即把 `embed_query` 提交到线程，SQLite 通道跑完再取结果；超时/降级语义不变。
  收益 ≈ `min(SQLite 通道耗时, embedding RTT)`。
- **注意**：`retrieval/vector.py:162` `_call_with_timeout` 每次新建线程池，超时后后台线程仍在跑（持续超时时线程堆积），可顺手改为模块级复用池。

### L4 本地 ONNX 首查询无 warm-up

- **证据**：`benches/results/robustness-scale.md` §A：首次 query embedding 含 ~5s 模型懒加载，正好撞上 `vector_timeout_s=5.0`
  → 每个新进程第一条查询静默降级为 Exact+BM25；代码中至今无 warm-up（`grep -i warm` 无结果）。
- **判断**：只影响本地 ONNX 形态（云端默认 API provider 不受影响），成本极小，**值得顺手做**。
- **方向**：service 启动 / `Engine.open` 后台触发一次 `embed_query`；CLI 一次性命令同理或放宽首调超时。

### L5 `spec_references` 缺 `spec_block_id` 普通索引（契约）

- **证据**：`storage/schema.sql:99` 只有 `WHERE stale = 1` 的**部分索引**；`Store.spec_refs_for_spec`（`WHERE spec_block_id = ?`，不带 stale 条件）
  用不上它 → 全表扫描；被 rerank（每个 spec 候选）与 expand（每个 spec seed）调用。
- **方向**：新增 `CREATE INDEX ... ON spec_references(spec_block_id)`。改的是冻结 DDL（`docs/contracts/index-schema.sql`），
  且 `validate_schema_version` 不做迁移 → 走契约流程，评估能否以“附加索引、不升 schema_version”的方式落地。
- **判断**：文档量大的仓库才明显；**排在 L1/L2 之后**。

---

## 4. 同步与索引

### 4.0 冷启动索引耗时：已有结论（用户深度研究，2026-09-26，沉淀于此）

**问题拆分**：索引耗时由三段组成——

| 段 | 主要受限于 |
|---|---|
| 本地预处理与后期存储（解析/切分/SQLite/FTS/LanceDB 落库） | CPU 性能 |
| Embedding 请求 | TPM、Batch 大小、并发数、带宽 |
| 向量结果下行 | 带宽、线程交替 |

**当前方案**：**2 Consumer × 每 Consumer 4 Batch 并发**（`ZACE_EMBED_WORKERS=2`、`EMBED_CONCURRENCY=4`）。

**各旋钮的决定因素**：

- `Chunk Size`：主要由**检索质量**决定（不是性能旋钮；改小会让 ask 更浅，见 `qa-quality-v1.md` §4-6）；
- `Batch Size`：主要由 Provider 单请求限制、请求效率、延迟和内存决定；
- `Batch Concurrency`：主要由 TPM/RPM、网络带宽和 API latency 决定；
- `Consumer 数量`：用于让网络 I/O 与本地解码/落库**流水化重叠**，数量受 CPU、内存和锁竞争限制。

流水线示意：[Pipeline 多消费者](https://pub-937eb406835a4e0b92163d9b18b9dbe4.r2.dev/2026/09/f709d36b40a23cb3f648db847e922622.png)。
实测与负结果（“窗口不能按消费者切小”）见 `benches/results/index-perf-task115-vps.md`、调优档见 `configs/profiles/README.md`。
**结论：本地 ingest 路径已接近 API 配额地板（langchain 3.56M token ÷ 16M TPM ≈ 13.4s），不再列为待办；剩余可做的是下面 S1/S2（生产路径）。**

### S1 远端同步路径 ≠ 基准路径（高优先，需实测对照）

- **证据**：
  - TASK-114/115 的冷启动数字来自 `zace-core ingest`（整仓一次 `Indexer` 运行、窗口 2000 chunk）；
  - 真实接入 `npx zace-client` 走：`client/src/remote.rs:191` 按 ≤1MB **串行**分批上传 →
    `service/zace_service/routers/sync.py:82` `batch_upload` 在**请求内同步** `manager.ingest`（解析 + embedding + 落库）→
    每批是一次独立的 `Engine._ingest`（开库、起 sink 线程、flush、`index_state` building→ready）。
- **推断**（待实测证实）：1MB 源码通常不足一个 2000-chunk 窗口 → K=2 消费者重叠基本吃不到；批与批之间串行，网络等待不重叠；
  各批之间 `index_state` 先后被标 ready，查询会看到“ready 但不完整”的索引。
- **方向**：`batch-upload` 只做“落 blob + 记账本 + 入队”，立即返回；服务端按项目**合并/去抖**成一次后台 ingest
  （复用 `ProjectIndexer` 的单线程模型）；`sync/status` 报进度。这同时兑现 D-30“上传完成 ≠ 索引完成”。
  涉及 CF-05 响应语义（`report` 字段）→ **契约**。
- **验证**：langchain 用真实 client 冷启动 vs `zace-core ingest` 的墙钟对照（先量差距，差距小就降级为 P2）。

### S2 D-31“首同步 120s 转后台”未实现

- **证据**：`client/src/tools.rs:226` `sync_project` 阻塞到所有批上传完成才检索；client 内无后台/120s 逻辑；
  单批请求超时 `UPLOAD_TIMEOUT=30s`（`remote.rs:30`），服务端单批 embedding 超过 30s 时客户端报错而服务端仍在写。
  `HANDOFF.md` §7 已提示“首次提问会等几分钟”。
- **方向**：依赖 S1 的异步 ingest；client 在首同步超过阈值时返回“索引中”包（`freshness.indexingFiles` + 进度），后续调用自然变新鲜。

### S3 LanceDB 从不 compaction

- **证据**：`core/zace_core/vectors/store.py` 无 `optimize` / `compact_files` / `cleanup_old_versions` 调用；
  每次增量同步的 `merge_insert` / `delete` 都产生新 fragment 与新版本（lancedb 0.38）。
- **判断**：短期无感，长期运行的活跃项目会**磁盘膨胀 + 暴力检索扫描更多碎片**。成本小，**值得做**。
- **方向**：ingest 结束后按碎片数/版本数阈值触发 `table.optimize()`（含旧版本清理）；放在后台索引线程里，不在查询路径上。
- **验证**：对同一项目连续 100 次小增量后，比较目录体积、fragment 数与 `search` 耗时。

---

## 5. 服务架构

### A1 索引与查询同进程（观察项）

- **证据**：`service/zace_service/indexer.py` 每项目一个 `threading.Thread`，与 uvicorn 查询共享进程与 GIL；
  TASK-115 报告已记录 GIL 争用；服务端峰值 823MB（`HANDOFF.md` §8-13）。
- **判断**：单用户 / 少量用户阶段**不值得**拆；触发条件：并发活跃用户增多，或大仓库索引期间查询 P95 明显劣化。
- **方向**：独立的索引 worker 进程（metadb 里加 job 表，service 只入队）——与 S1 的“入队”天然衔接，S1 做好后成本会降低。

### A2 `/healthz` 免鉴权列出全部项目进度

- 已在 `HANDOFF.md` §8-2 登记，2026-10-04 复核仍在（`service/zace_service/routers/ops.py:49/64`）。方向：免鉴权只回存活与依赖状态，项目进度移到鉴权端点。

---

## 6. 可维护性与文档

### M1 超大文件（观察项，随功能卡顺手拆）

| 文件 | 行数 | 混在一起的职责 | 建议切分 |
|---|---|---|---|
| `core/zace_core/contextpack/assembly.py` | 1669 | 装填贪心 / 降级与合并 / 可回答性评估 / 缺口与 next_queries / JSON 序列化 | `packing.py` / `assess.py` / `gaps.py` / `serialize.py` |
| `core/zace_core/engine.py` | 1433 | 仓库身份 / 扫描 / ingest / 两轮检索 / manifest | 身份与扫描移出；`search_with_trace` + `_backfill_gaps` 独立成 `search.py` |
| `service/zace_service/metadb.py` | 1828 | 用户 / token / 审计 / 统计 / 配额 / 邀请码 | 按领域拆 repository |
| `core/zace_core/pipeline/ignore.py` | 1112 | — | 已有 Rust 侧对拍测试（`client/tests/allowlist_parity.rs`），拆分时保持对拍 |

不建议单独开“纯重构卡”；在 Q3/Q4/S1 等触及这些文件时顺带拆，保持行为不变（ContextPack 可逐字节 diff）。

### D1 G3/G4 命名漂移（交编排者）

- `core/zace_core/retrieval/gap.py:19` 模块 docstring 仍写“两条规则；G3/G4/G5 见明确不做”，但同文件已实现 G3（容器成员被调用方）与 G4（公开导出点）；
- `docs/design/INDEX.md` D-19 写“G3 按 I1 不合法、未实现”，与代码中 G3 的含义不同。
- 实施 AI 不得改 `docs/design/**`：由编排者决定是更新 Module/02 §4.7 的规则表，还是把代码里的规则改名；docstring 随之更新。

---

## 7. 明确判断为“暂不优化”的点

| 点 | 理由 |
|---|---|
| 向量 ANN 索引（IVF/HNSW） | 当前规模（≤ 数万 chunk）暴力检索 ~20–30ms（`robustness-scale.md` §A），远小于 embedding 往返；>20 万 chunk 再评估 |
| 每查询重新打开 SQLite / LanceDB（`engine.py:1282`） | 打开成本毫秒级，缓存句柄会引入 ingest 后的失效问题；**仅当 L1 的 profile 显示它占比显著时**再做 |
| 换 embedding 模型 / 维度 | 已有 bakeoff 结论；维度不可配是已知缺口（`baseline-v1.md` §5-1），属功能而非优化 |
| Cross-Encoder / HyDE / 多查询改写 | 任务板“已明确不做 / V1.5”，延迟预算不允许 |
