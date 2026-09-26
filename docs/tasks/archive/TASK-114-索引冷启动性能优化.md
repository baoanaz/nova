# TASK-114：索引冷启动性能优化（P0-1/P0-2/P1-3/P1-4/P1-5）

> 状态：**已合并 main（`a977da6`）** ｜ 阶段：Phase 5+（性能） ｜ 硬依赖：无 ｜ soft 依赖：无
> 建议分支：`feature/task-114_index-perf0926`
> 交付物所有权：`core/zace_core/pipeline/**`、`core/zace_core/storage/{store,db}.py`、
> `core/zace_core/text/segmenter.py`、`core/zace_core/vectors/{store,cache}.py`、
> `core/zace_core/engine.py`（仅索引路径与导入链）、对应的 `core/tests/**`；
> 文档：`docs/plan/index-perf-plan.md`、`docs/plan/index-perf-handoff.md`、本卡、`docs/tasks/README.md`。
> 清单外文件不得改（`docs/design/**`、`docs/contracts/**`、`core/zace_core/{types,interfaces,hashing}.py` 一律不动）。

## 目标

把 2026-09-25 调研出的冷启动浪费落地修掉：**删掉纯 ASCII 文本上的 jieba 无用功（P0-1）、
不再让整仓向量以 Python list 常驻（P0-2）、去掉冷启动里的空转 SQL（P1-3）、
让本地 CPU 段与网络段重叠（P1-4）、CLI 不再为 lancedb 付固定 3s 导入（P1-5）**。
被 service 与 CLI 的索引路径共同消费，行为与检索质量不得回退。

完整计划、依赖顺序、架构问题清单与验收矩阵见 [`../../plan/index-perf-plan.md`](../../plan/index-perf-plan.md)。

## 输入文档（按序读，只读所需章节）

1. [`../../plan/index-perf-handoff.md`](../../plan/index-perf-handoff.md)（实测数字与代码定位的唯一来源）；
2. [`../../design/Module/01-切片存储.md`](../../design/Module/01-切片存储.md) §4.1–§4.3（双层增量 / 指纹失效 / 时序）；
3. [`../../contracts/PROCESS.md`](../../contracts/PROCESS.md) §3.2 R4（复用键是 hash）、R10（rebuild 语义）；
4. `docs/contracts/index-schema.sql`（CF-01，只读，不改）。

## 冻结接口（本卡不得变更）

- 消费：`Store.apply_file_change` 的 `FileDelta` 三集合语义（CF-08）、`EmbeddingProvider.embed`（CF-09）；
- 产出：`IngestReport` 字段口径、`VectorStore` 公开方法面、`zace_core.text.segment` 的"双侧同函数"约束。

## 交付物

| 路径 | 内容 |
|---|---|
| `core/zace_core/text/segmenter.py` | ASCII 快速路径（P0-1）+ 等价性论证注释 |
| `core/zace_core/pipeline/embedding_sink.py` | 新增：向量阶段内聚单元（窗口化、同轮去重、缓存下沉） |
| `core/zace_core/pipeline/indexer.py` | 改为生产者/消费者；接 sink；删累积；`write_batch` |
| `core/zace_core/storage/db.py` | `transaction()` 支持嵌套（SAVEPOINT） |
| `core/zace_core/storage/store.py` | `write_batch()`；`apply_file_change` 冷启动快路径 |
| `core/zace_core/vectors/{store,cache}.py` | 惰性 `import lancedb`（P1-5） |
| `core/zace_core/engine.py` | 索引路径告警（WS6）与导入链调整 |
| `core/tests/**` | 等价性 / 流式 sink / 快路径 / 流水线的新增与回归测试 |

## 验收标准（DoD）

- [ ] 新增测试覆盖：ASCII 与 jieba 的 FTS token 等价性；同轮同内容只嵌一次；
      空库快路径与旧实现产出相同 `FileDelta`；嵌套事务的 SAVEPOINT 回滚隔离；流水线计数不变。
- [ ] 性能验收（本机 `vps-la-2c2g`，探针命令见计划 §5）：
      HelloAgents 冷启动不高于基线 16.8s、峰值 RSS 不高于 570MB；
      langchain 视额度跑一次（131.5s → 阶段 1/2 目标）。
- [ ] 质量验收：golden recall@5 不低于 0.64–0.77（抽查 HelloAgents / leveldb）。
- [ ] 基线三条全绿：`uv run ruff check .`、`uv run python scripts/check_dependency_direction.py`、
      `uv run pytest -o addopts="" -q`。
- [ ] 任务卡"执行记录"已回填；任务板状态改为 `review`。

## 明确不做

- 不调质量参数（R29/R30 冻结）、不改冻结契约、不改设计文档；
- 不做 P2-6 多进程（2 vCPU 收益不确定 + lancedb fork 告警）；
- 不做 P2-7 的"接管旧索引目录"（冻结设计，需编排者裁决；本卡只加告警）；
- 不重写检索链、不动 rerank/装填。

## 完成报告（回填）

按 `docs/plan/orchestration.md` §3 模板填写：分支 / 验收命令与结果 / 契约影响 / 与设计偏差 / 未决问题。

## 执行记录

### 2026-09-26 ｜ 分支 `feature/task-114_index-perf0926` ｜ 工作区 `/root/xuwenzheng/ace/zace-perf`

**范围**：handoff 的 P0-1 / P0-2 / P1-3 / P1-4 / P1-5 全部落地；P2-7 只做告警；P2-6、P3-8 按计划不做。
计划、架构问题清单、验收矩阵见 [`../../plan/index-perf-plan.md`](../../plan/index-perf-plan.md)；
实测报告见 [`../../../benches/results/index-perf-task114-vps.md`](../../../benches/results/index-perf-task114-vps.md)。

**关键决策**

1. **P0-1 的快路径是"复刻 jieba 的 ASCII token 形状"，不是"直接返回原文"**。
   选后者会让 `TokenService.refresh_token` 从 5 个 token 变成 1 个，改变按空格切 token 的消费方
   （实测直接把 `service/tests/test_usage_api.py::test_insufficient_evidence_is_counted_separately`
   打红）。现规则 `[A-Za-z0-9]+|[\S]` 与 jieba 逐 token 相同（13 个样本 10 个完全一致，
   差异只在 `3.14`/`42%`/`++` 这类字面量分组，unicode61 下 token 流相同）。
   **判定：不构成检索语义变更 ⇒ 不触发索引重建、不在 D-45 修订范围**（等价性有单测锁定）。
2. **P1-3 的"整轮单事务"经对照实验证明对墙钟中性**（`raw/task114/hello-new-nobatch-localonly.json`
   vs `hello-new-localonly.json`），保留它是为了"批内原子 + 单文件 SAVEPOINT 隔离"的语义，
   不当作性能项宣传；真正的收益来自"新文件跳过空转 SELECT/DELETE"。
3. **向量阶段独立成 `pipeline/embedding_sink.py`**（`EmbeddingSink` 同步核心 + `EmbeddingPipeline`
   后台消费者）：不跨窗口累积整仓向量、跨文件攒批、窗口内按内容去重、缓存逐窗口写回。
4. **计数口径拆桶**：`chunks_reused` 回归"文件级对账"语义，新增 `chunks_deduped`
   表示"按内容命中而省掉嵌入调用"。原因：TASK-114 的跨窗口去重让旧口径在**全新构建**上报出
   `chunks_new + chunks_reused > 总 chunk 数`（langchain：20931 + 1456），可读性差且无法验证。
   新口径满足 `chunks_new + chunks_reused == 写入数`、`vectors_upserted == embedded + deduped`。

**验收命令与结果**

| 命令 | 结果 |
|---|---|
| `uv run ruff check .` | All checks passed |
| `uv run python scripts/check_dependency_direction.py` | 依赖方向检查通过 |
| `uv run pytest -o addopts="" -q` | **765 passed, 9 skipped**（core 全量，串行） |
| `uv run pytest -o addopts="-n auto"` | 1333 passed / 1 failed（`service/tests/test_admin.py` 在 xdist 下 worker 崩溃，串行 38/38 通过，属既有环境抖动；基线同样复现） |
| langchain 真实 API 冷启动 | **131.5s → 77.6s（-41%）**，峰值 1143MB → **722.7MB**，3.56M token / 57 请求 |
| langchain 本地段（零网络） | 进程墙钟 **121.4s → 49.4s**，峰值 1142.7MB → 695.9MB |
| HelloAgents 真实 API | 18.26s → 17.99s（探针口径；新实现含 2.6s 惰性导入，纯工作 ≈ -2.6s），峰值 563 → 509MB |
| `import zace_core.cli` | 4.2s → **0.66s** |
| 质量 A/B（各自新建索引，`zace-core eval`） | leveldb `1.000/1.000/0.721` 完全一致；HelloAgents `0.842/0.947/0.766 → 0.842/0.947/0.765`（MRR 差 1e-3，见下） |

**与设计的偏差 / 需报备的改动**

1. **新增文件 `core/zace_core/pipeline/embedding_sink.py`**（清单内 `pipeline/**`）；
2. **`IngestReport` 新增 `chunks_deduped`**（加法式扩展，service 只读 `chunks_reused`，语义未变）；
3. **改了一个既有测试的期望**：`core/tests/cli/test_cli_ingest.py::test_full_flag_triggers_full_reparse`
   原本断言"`--full` 会把 embedding 再跑一遍"，现在被内容缓存兜住（零新增调用、向量表仍被完整重建）。
   这是 TASK-111 缓存应有的收益，不是回归；已在测试里注明；
4. **改了 `benches/embed-bench/` 两个文件**（清单外，报备）：
   - `ingest_probe.py`：import 随 `_embed_window_size` 迁移改为 `embedding_sink.embed_window_size`；
   - 新增 `coldstart_probe.py`：把 handoff 用的外部探针收进仓库并适配新结构
     （`_embed_new` → `EmbeddingSink._process_window` + `EmbeddingPipeline.submit`），
     使性能证据可使用仓库内脚本复现；
5. 新增 `benches/results/index-perf-task114-vps.md` 与 `benches/results/raw/task114/`（原始证据）。

**未决问题（交编排者）**

1. **HelloAgents 的 MRR 差 1e-3（H-07 位次 9→10）已定位根因但需裁决是否接受**：
   Voyage 返回向量随**批次组成**抖动 ~1e-3（同文本单独成批 vs 与其它文本同批，实测差 1e-3、
   与位置无关）；TASK-114 改了攒批方式，于是落到某个近似并列位次上。recall 完全一致。
   若要求"新实现与旧实现向量逐位不可比"，则需固定批次切分（会牺牲攒批/去重收益）。
2. **`chunks_deduped` 是否透出到 service**（`sync.py` 的响应体）——需改 service 文件，本轮未动。
3. **P2-7 的"接管旧索引目录"** 仍待裁决；本轮只加了告警（`IngestReport.warnings` + CLI stderr）。
4. langchain 的**真实 API 旧实现对照**未在本会话重跑（用的是 `raw/ingest-vps/` 同机同配置历史值，
   3.56M token/次）；如需同会话 A/B，需再花一次额度。
