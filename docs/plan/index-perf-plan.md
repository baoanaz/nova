# 索引冷启动性能优化计划（TASK-114）

> 输入：本文档目录下的 [`index-perf-handoff.md`](index-perf-handoff.md)（2026-09-25 本机实测调研）。
> 任务卡：[`../tasks/TASK-114-索引冷启动性能优化.md`](../tasks/TASK-114-索引冷启动性能优化.md)。
> 工作区纪律见根 `AGENTS.md` §0（本会话独占 `zace-perf` worktree，主工作区只做集成）。

---

## 0. 目标锚点（本会话结束时必须逐条回答）

**一句话目标**：把冷启动里"本地 CPU 段 ↔ 网络段严格串行、零重叠"这件事拆开，并删掉无效工作，
使 langchain 冷启动 131.5s → 阶段 1 ~75s → 阶段 2 ~35s，**且检索质量不回退**。

三条不许越界（写在这里，避免实施到后半段被"顺手优化"带偏）：

1. **不基于现有 smoke/golden 集调参**（R29/R30 冻结）；本轮只改"怎么算"，不改"算多少"。
2. **不改冻结契约**：`docs/contracts/**`、`core/zace_core/{types,interfaces,hashing}.py` 一律不动；
   需要改就写进任务卡"未决问题"停下来。
3. **不改设计文档**（`docs/design/**`），发现设计冲突写进任务卡"执行记录"。

## 1. 待办全景（来源：handoff §3，按依赖排序）

| 编号 | 内容 | 预期收益（langchain） | 依赖 | 本轮回合 |
|---|---|---|---|---|
| P0-1 | jieba 对纯 ASCII 文本做无用功 | -36.4s | 无（但需语义论证） | **WS2 做** |
| P0-2 | 整仓向量以 Python list 常驻 | -700MB 峰值 | 无（是流水线前提） | **WS3 做** |
| P1-3 | 冷启动"定位旧行/删旧行/每文件独立事务" | -8~11s | 无 | **WS4 做** |
| P1-4 | 本地与网络严格串行、零重叠 | -28% 墙钟 | **必须先做 P0-2** | **WS5 做** |
| P1-5 | CLI 固定导入 3.85s（lancedb 链） | 所有 CLI 口径 -3s | 无 | **WS1 做** |
| P2-6 | 解析/解码下多进程 | 本地段再 -30~50% | P0-1/P0-2 落地后 | **延后**（见 §6） |
| P2-7 | 索引身份变更触发全量重嵌 | 防 4.58M token 白烧 | 冻结设计，需编排者裁决 | **WS6 只加告警** |
| P3-8 | 硬件采购判据 | — | 代码优化先做完 | **只复述判据** |

## 2. 实施顺序与依赖图

```text
WS1 惰性 lancedb ────────────────┐（独立，可先做）
WS2 segment ASCII 快速路径 ──────┤
WS3 向量流式 sink（P0-2+架构）───┼─→ WS5 本地/网络流水线（P1-4）
WS4 冷启动 SQL（P1-3）───────────┘
WS6 索引位移告警（P2-7 弱化版）───（独立）
```

- WS3 必须先于 WS5：否则流水线会把峰值内存再推高（handoff §3 明确）；
- WS1/WS2/WS4 与 WS3 互不冲突，但为了"每步都可回归"，实施仍按编号串行；
- 每完成一个 WS：跑基线三条 + 相关单测，再进入下一个。

## 3. 架构问题清单（本轮一并处理，理由见各 WS）

| # | 问题 | 处理 |
|---|---|---|
| A1 | `pipeline/indexer.py` 是 686 行上帝类：输入收集 / 单文件入库 / 向量对账 / 重建 / 二阶段解析全揉在一起 | WS3 把"向量阶段"拆成独立内聚单元 |
| A2 | `db.transaction()` 显式拒绝嵌套，"多文件一个事务 + 单文件失败隔离"没有着力点 | WS4 改为**嵌套即 SAVEPOINT** |
| A3 | `_embed_and_upsert` 跨窗口累积整仓 `produced`；`_rebuild_vectors` 拿完直接丢弃 | WS3 删除累积，边嵌边写边回缓存 |
| A4 | `_embed_new` docstring 声称"本轮新嵌进 fresh 池，同内容只嵌一次"，实现里 `produced` 根本没参与去重 | WS3 真做同轮内容去重 |
| A5 | `_known_chunks` 只服务于 `orphan_files` 排查信号，却与向量正确性耦合过深 | WS3 保留信号语义、与清理彻底解耦（已是现状，补注释与测试） |
| A6 | 向量存储/缓存的模块级 `import lancedb` 把 3.16s 强加给每个 CLI 调用 | WS1 惰性导入（`TYPE_CHECKING` + 运行时 loader） |

## 4. 各 WS 的设计与验收

### WS1 — 惰性 lancedb 导入（P1-5）

- **设计**：`vectors/store.py`、`vectors/cache.py` 不再在模块顶层 `import lancedb`；
  改为一个 `_lancedb()`（`lru_cache`）在每个类方法入口按需取。类型注解走 `TYPE_CHECKING`。
- **不做**：不动 `pyarrow`（它是 schema 定义，成本低）。
- **验收**：`time uv run zace-core --help`（或 `import zace_core.cli`）导入耗时从 ~3.9s 降到 ~1s；
  向量单测全绿（含 `test_store.py` / `test_cache`）。

### WS2 — `segment()` ASCII 快速路径（P0-1）

- **设计**：`segment(text)` 在空白判定之后，若 `text.isascii()` 直接返回原文。
  理由不是"猜"，而是可证明的 token 等价：`unicode61` 对 ASCII 只在非字母数字处切分，
  而 jieba 对 ASCII 只做"插入分隔符"，二者产出的 **FTS token 流完全相同**。
- **关键正确性论证**（写进单测，长期守住）：
  - 对任意纯 ASCII 文本，`terms(unicode61(jieba(text))) == terms(unicode61(text))`；
  - 混合场景（文档含 CJK / 查询纯 ASCII，或反之）两侧 token 空间仍一致：
    含 CJK 一侧走 jieba 时，ASCII 片段的 token 与快速路径相同。
  - 反例仅出现在"非 ASCII、非 CJK 的字母"（如 `café`）：这些文本 `isascii()=False`，仍走 jieba，
    因此与旧行为逐字节一致。
- **不做**：不改 D-45 的"双侧同函数"约束（仍然同函数，只是函数内部有等价快路径）；
  不需要重建索引（token 流不变）。
- **验收**：新增等价性单测；`bench/golden` 抽查不回退；local-only 探针里 `sqlite.jieba_segment` 大幅下降。

### WS3 — 向量阶段下沉为流式 sink（P0-2 + A3/A4）

- **设计**：新模块 `core/zace_core/pipeline/embedding_sink.py`，一个内聚单元只负责
  "chunk → 向量表 + 缓存"这条链：
  - `submit(chunks)` 接收一段 chunk，内部按 `窗口 = batch_size × concurrency` 处理；
  - 每个窗口：`get_hashes`（同 id 同内容）→ 同轮 `hash → vector` 内存索引 → 缓存 lookup →
    剩余 `embed()` → `vectors.upsert` → **本窗口立刻写回缓存**；
  - 不返回、不累积任何整仓级向量；`IngestReport` 计数通过回调/累加器回写。
- **语义约定**：缓存是优化，任何失败降级为未命中（沿用 TASK-111 口径）；
  本轮新增"同轮同内容只嵌一次"是行为改进（旧 docstring 已承诺、实现未兑现）。
- **`_rebuild_vectors`** 改为不 collect：`reembed/full_reparse` 路径与增量共用同一 sink。
- **验收**：`coldstart_probe.py` 的 `peak_rss_mb` 显著下降（langchain 目标 < 500MB）；
  `chunks_reused` / `vectors_upserted` 计数与原口径一致（单测 + `test_branch_identity_reuse`）。

### WS4 — 冷启动 SQL 快路径（P1-3 + A2）

- **设计 A2**：`storage/db.py::transaction()` 支持嵌套：已在外层事务内时用 `SAVEPOINT/RELEASE/
  ROLLBACK TO`，否则 `BEGIN IMMEDIATE`。这样"外层一个大事务 + 每个文件一个 SAVEPOINT"
  天然获得**单文件失败隔离**（TASK-018 §C 语义不变）。
- **设计 B**：`Store.write_batch()` 上下文管理器（`transaction()` 薄封装），`Indexer` 用它把
  一整轮文件的写包进一个事务（省 ~3.3s 的 COMMIT）。
- **设计 C**：`apply_file_change` 先用 `files` 主键探测本文件是否存在：
  不存在 → 跳过 2 条旧行 SELECT、4 条 DELETE、FTS rowid SELECT 与 edge 源 DELETE 之外的全部删除。
  不变量：`chunks/symbols/spec_blocks/unresolved_refs` 的行必然伴随 `files` 行存在。
- **验收**：`sql_replay.py` 风格回归（空库 per-file 固定开销下降）；存储/索引单测全绿；
  增量路径 `FileDelta` 与旧实现逐字段相等。

### WS5 — 本地/网络流水线（P1-4）

- **设计**：`Indexer._run` 变成生产者/消费者：
  - 主线程（生产者）：读文件 → 解析 → 切分 → `store` 写入 → 把新 chunk 投入 sink；
  - 消费者：单后台线程持有 `EmbeddingSink`，网络在飞 + 向量落库 + 缓存写回；
  - 有界队列（容量 = 一个窗口），背压天然限内存；
  - 消费者异常必须回抛主线程（不静默）；关停时排空队列再返回。
- **不做**：不做多生产者/多消费者；SQLite 写仍单线程（per-project 单写者不变量）。
- **验收**：探针里 `network_busy_s / wall_s` 比值上升（>0.3），CPU 利用率 >90%（2 核口径）；
  全量 pytest 绿；`test_pipeline_real_stack` 的计数断言不变。

### WS6 — 索引位移告警（P2-7 弱化版）

- **设计**：`Indexer` 在指纹判定为 `full_reparse` 且**库内已有行**时，报告里给出一条可观测告警
  （`IngestReport` 现有字段之外的显式日志 + CLI 打印），提示"可能是身份位移，将全量重嵌"。
  **不**实现"接管旧索引"（属 D-29/TASK-111 冻结设计，需编排者裁决）。
- **验收**：单测断言告警只在"已有行 + full_reparse"时出现；CLI 输出可读。

## 5. 验证矩阵

```bash
# 基线三条（每次提交前）
uv run ruff check .
uv run python scripts/check_dependency_direction.py
uv run pytest -o addopts="" -q

# 性能证据（本机 vps-la-2c2g 口径；探针已收进仓库）
uv run python benches/embed-bench/coldstart_probe.py \
  --repo /root/xuwenzheng/ace/benchmark/HelloAgents --data "$(mktemp -d)" \
  --out /tmp/probe-hello.json --mode local-only

# 质量回归（不回退）
uv run zace-core eval --golden benches/golden/HelloAgents/HelloAgents.jsonl \
  --project-id <projectId> --data <数据根> --report /tmp/eval.md
```

| 判据 | 阈值 |
|---|---|
| ruff / 依赖方向 / pytest | 全绿 |
| HelloAgents 冷启动墙钟 | 不高于基线 16.8s（预期 ~11s） |
| HelloAgents 峰值 RSS | 不高于 570MB（预期 ~300MB） |
| langchain（若跑） | 131.5s → 阶段 1 ~75s / 阶段 2 ~35s |
| golden recall@5 | 不低于 0.64–0.77 |

**实测结果（2026-09-26，全部归档在 [`benches/results/index-perf-task114-vps.md`](../../benches/results/index-perf-task114-vps.md) 与 `benches/results/raw/task114/`）**：

- langchain 真实 API：131.5s → **77.6s（-41%）**；本地段进程墙钟 121.4s → **49.4s**；峰值 RSS 1143MB → **696MB**；
- HelloAgents：进程墙钟 17.8s → 14.2s；`apply_file_change` 9.2s → 6.9s、jieba 8.0s → 6.1s；
- `import zace_core.cli` 4.2s → **0.66s**；
- 质量 A/B（各自新建索引）：leveldb recall@5 1.000/1.000、HelloAgents 0.842/0.842；MRR 仅差 1e-3，已定位为 Voyage 批次相关抖动。

## 6. 明确不做 / 延后（写到"不做"才会不做）

- **P2-6 解析/JSON 解码多进程**：收益依赖 ≥4 核，且 lancedb 在 fork 下有已知告警、
  spawn 下每个 worker 重付 ~3.5s 导入；本机 2 vCPU，收益不确定、复杂度高 → 延后到换机后。
- **P3-8 硬件采购**：本轮只把判据复述进报告，不做采购建议变更。
- **索引身份"接管旧目录"**：冻结设计，等编排者裁决（WS6 只做告警）。
- **质量参数（docs_ratio / rerank 权重）**：R29/R30 冻结，不动。

## 7. 进度勾选（长程防走丢）

- [x] WS0 规划落地（本文件 + 任务卡 + 任务板）
- [x] WS0 基线跑通（ruff / 依赖方向 / pytest）
- [x] WS1 惰性 lancedb（4.2s → 0.66s）
- [x] WS2 segment ASCII 快速路径 + 等价性单测（FTS token 流不变）
- [x] WS3 向量流式 sink（跨文件攒批、窗口内去重、rebuild 不 collect）
- [x] WS4 嵌套事务 + `write_batch` + 冷启动 SQL 快路径
- [x] WS5 生产者/消费者流水线（后台嵌入 + 有界队列背压）
- [x] WS6 索引位移告警
- [x] 性能复测（探针，新旧同机对照）+ 质量 A/B（各自新建索引）
- [x] 回填任务卡执行记录 / 任务板状态 / 提交（本地，不 push）

## 8. 风险与回退

| 风险 | 缓解 |
|---|---|
| 2 GiB 机器跑 langchain 触发 OOM | WS3 先落地降峰值；langchain 复测用 `systemd-run` 内存护栏；必要时只用 HelloAgents 出证据 |
| 流水线引入非确定顺序 | 单消费者 + 队列 FIFO；向量写入与 id 顺序无关（按 id 幂等） |
| 大事务导致 WAL 增长 | 记录 WAL 体积；单文件 SAVEPOINT 保证失败隔离；必要时按文件数分批提交 |
| 长事务跨嵌入阶段持锁 | 收集/解析/写库与嵌入消费分离，提交在写库结束后；嵌入读的是 LanceDB，不争 SQLite 写锁 |
| ASCII 快速路径语义被质疑 | 附等价性单测 + 探针数据；任务卡"执行记录"写清判定依据供编排者复核 |
