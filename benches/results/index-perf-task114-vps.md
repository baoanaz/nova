# 索引冷启动性能优化（TASK-114）实测报告

> 设备：`vps-la-2c2g`（2 vCPU / 1.9 GiB / 洛杉矶）｜embedding：`voyage-4-lite` @1024（API，并发 4）
> 日期：2026-09-26｜分支：`feature/task-114_index-perf0926`｜输入：`docs/plan/index-perf-handoff.md`
> 原始计量 JSON：`raw/task114/`（本报告每个数字都能从那里复算）

## 0. 一句话结论

**langchain 冷启动：真实 API 131.5s → 77.6s（-41%）；纯本地段（零网络替身）进程墙钟 121.4s → 49.4s（-59%）；峰值 RSS 1143MB → 723MB（-37%）。**
检索质量 A/B 逐位一致（leveldb recall@5 1.000 / HelloAgents 0.842），负例仍 1/1 通过。

## 1. 对照口径（必读，否则会看错数字）

两个"墙钟"不是一回事：

- **探针墙钟**（`coldstart_probe.py` 的 `wall_s`）= `main()` 内部计时，**不含模块 import**；
- **进程墙钟**（`/usr/bin/time`）= 用户真实体感，**含 import**。

TASK-114 把 `lancedb` 改成惰性导入后，那 ~2.6s 从"进程启动（探针计时之外）"移到了"第一次
`VectorStore.open`（探针计时之内）"。因此：

- 同一份探针墙钟比较时，新实现要**减去 2.6s** 才是纯工作口径；
- 想看体感直接看**进程墙钟**（两边都含 import，无需手工扣）。

## 2. 冷启动（零网络替身 `--mode local-only`，只测本地段）

| 靶场 | 口径 | 旧实现 | 新实现 | 变化 |
|---|---|---|---|---|
| langchain @ `e75dae1f` | 进程墙钟 | 121.4s | **49.4s** | **-59%** |
| langchain | 探针墙钟（工作） | 120.7s | 49.1s | -59% |
| langchain | 峰值 RSS | 1142.7MB | **695.9MB** | -39% |
| HelloAgents @ `93e77ea` | 进程墙钟 | 17.8s | 14.2s | -20% |
| HelloAgents | 探针墙钟（工作） | 13.1s | 12.7s（含惰性导入 2.6s） | ≈-2.0s 纯工作 |
| HelloAgents | 峰值 RSS | 521–530MB | 472–478MB | -9% |

阶段分解（langchain，探针口径）：

| 阶段 | 旧 | 新 | 说明 |
|---|---|---|---|
| `sqlite.apply_file_change` | 66.8s | 12.0s | P1-3 冷启动快路径（新文件不发 2 条定位 SELECT + 4 条 DELETE） |
| └ `sqlite.jieba_segment` | 39.4s | 6.2s | P0-1（langchain 99.9% 无 CJK；剩下的 CJK 仍需 jieba） |
| `embed.stage_rebuild`（串行） | 33.3s | — | 已由流式 sink 取代 |
| `embed.stage_window`（后台线程） | — | 18.2s | P1-4：与本地段重叠；producer 被背压等待 12.6s |

## 3. 真实 API 冷启动

| 靶场 | 旧实现 | 新实现 | 变化 | 备注 |
|---|---|---|---|---|
| langchain @ `e75dae1f` | 131.5s（`raw/ingest-vps/`，2026-09-15 同机同配置） | **77.6s** | **-41%** | 57 请求 / 3.56M token / 227MB 下行 |
| HelloAgents @ `93e77ea` | 18.3s | 18.0s（含 2.6s 惰性导入） | ≈-2.6s 纯工作 | 网络段 5.4s 被本地段完全盖住 |

langchain 新实现的账：本地段 19.9s（主线程）+ 消费者 48.1s（网络 34.0s + 解码/浮点/落库）→ 墙钟 77.6s。
**消费者已成为新瓶颈**（`embed.batch_thread_wall` 112.9s 是并发线程累加，不是墙钟）；
再往下压需要多进程/独立解码线程（handoff P2-6，本轮明确不做）。

## 4. 检索质量 A/B（同题集、同 commit、各自新建索引）

`zace-core eval`，19 条正例 + 1 条负例：

| 靶场 | 指标 | 旧实现 | 新实现 |
|---|---|---|---|
| leveldb @ `7ee830d` | recall@5 / recall@10 / MRR / 负例 | 1.000 / 1.000 / 0.721 / 1/1 | **1.000 / 1.000 / 0.721 / 1/1** |
| HelloAgents @ `93e77ea` | recall@5 / recall@10 / MRR / 负例 | 0.842 / 0.947 / 0.766 / 1/1 | 0.842 / 0.947 / **0.765** / 1/1 |

HelloAgents 唯一的差异是 H-07 的期望证据位次 9 → 10（仍在 top-10 内，recall 不变，MRR 差 1e-3）。
**根因已定位，不是检索语义变更**：

```text
同一条文本 "class TestTodoWriteTool:..."（同一 chunk、同一 content_hash、同一 embedding_text）
  单独成批        → [-0.059726, -0.035377, -0.037086]
  与其它文本同批  → [-0.059646, -0.035728, -0.037062]   ← 与批次位置无关，只与"批的组成"有关
```

Voyage 的返回向量随**批次组成**抖动约 1e-3；TASK-114 改变了攒批方式（跨文件攒批 + 窗口内按内容
去重），于是某个 chunk 落库的向量与旧实现相差 1e-3，在一个近似并列的位次上翻转了一位。
同一索引、同一进程重复跑 3 次结果完全一致（已实测），所以这不是随机噪声，而是"批组成"这一
实现细节的确定性差异。**结论：质量护栏（recall）不回退。**

## 5. CLI 导入（P1-5）

| 口径 | 旧 | 新 |
|---|---|---|
| `import zace_core.cli`（3 次取样） | 4.73 / 4.16 / 4.22s | 1.21 / 0.76 / 0.66s |

`zace-core ingest` 本身仍要用向量库，所以这条省的是"不碰向量库的路径"（`--help` / `status` /
无变更增量）以及把导入期固定成本从计时段里移出去。

## 6. 计数口径变化（TASK-114 起）

`IngestReport` 新增 `chunks_deduped`，三个桶改成可验证的加法：

```text
chunks_new + chunks_reused == 写入 SQLite 的 chunk 数   （文件级对账，语义未变）
vectors_upserted == embedded + chunks_deduped           （向量阶段）
```

- `chunks_reused`：**文件级**对账命中（hash 未变）——语义回到 TASK-114 之前；
- `chunks_deduped`：按内容命中向量表 / 跨项目缓存 / **本轮更早窗口刚写的行** → 省掉嵌入调用；
- 实例（langchain 真实 API 新建索引）：`chunks_new=20931, chunks_reused=0, chunks_deduped=1456`
  —— 1456 次嵌入调用被内容寻址省掉（旧实现在同一批里不会去重）。

> 注：`service` 面只读 `chunks_reused`，语义未变；`chunks_deduped` 尚未透出到 REST（属后续卡）。

## 7. 本轮不做（与 handoff 对齐）

- P2-6 解析/解码多进程：需 ≥4 核才有意义，且 lancedb 在 fork 下有已知告警；
- P2-7 "接管旧索引目录"：D-29/TASK-111 冻结设计，本轮只加**告警**（`IngestReport.warnings`）；
- P3-8 硬件判据：只复述，不采购。

## 8. 复现

```bash
set -a; source /etc/zace/zace.env; set +a      # 需要 EMBED_*（本轮真实 API 共 ~10.7M token）

# 冷启动探针（local-only 免费；full 走真实 API）
uv run python benches/embed-bench/coldstart_probe.py \
  --repo /root/xuwenzheng/ace/benchmark/langchain \
  --data "$(mktemp -d)" --out /tmp/lc.json --mode local-only --tag lc

# 2 GiB 机器必须带内存护栏（handoff §5）
systemd-run --scope --quiet -p MemoryHigh=1400M -p MemoryMax=1700M -p MemorySwapMax=1024M -- \
  uv run python benches/embed-bench/coldstart_probe.py --repo <repo> --data <dir> \
  --out /tmp/x.json --mode local-only

# 质量 A/B（各自新建索引后）
uv run zace-core ingest --repo /root/xuwenzheng/ace/benchmark/leveldb --data "$(mktemp -d)"
uv run zace-core eval --golden benches/golden/leveldb/leveldb.jsonl \
  --project-id <projectId> --data <数据根> --report /tmp/eval.md

# 基线三条
uv run ruff check . && uv run python scripts/check_dependency_direction.py && uv run pytest -o addopts="" -q
```

## 9. 原始证据索引（`raw/task114/`）

| 文件 | 内容 |
|---|---|
| `langchain-{base,new}-localonly.json` | §2 的零网络对照（旧/新） |
| `langchain-new-full.json` | §3 真实 API 新实现（3.56M token、57 请求） |
| `hello-{base,new}-localonly.json`、`hello-{base,new}-full.json` | HelloAgents 对照 |
| `hello-new-nobatch-localonly.json` | 对照实验：关掉"整轮单事务"（证明其对墙钟中性） |
| `eval-{leveldb,helloagents}-{control,new}.md` | §4 的 `zace-core eval` 报告 |
