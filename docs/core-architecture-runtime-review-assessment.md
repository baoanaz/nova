# 对《zace-core 架构评审与 Evidence Runtime 建议》的复核

> 日期：2026-09-17｜复核对象：`docs/core-architecture-runtime-review.md`（下称"原评审"）
> 复核方式：逐条核对代码事实 + 在**当前 main** 上重跑 benchmark（`main @ 20346f8`）
> 性质：只读复核 + 跑分对照。不含实现改动，不改冻结设计与契约。

## 0. 结论

**原评审的事实核查可全数通过，但它的排期建议不采纳。**

- 抽查 12 条代码断言（含行号），**全部属实**，无编造、无张冠李戴——这份评审的
  事实质量高于一般 AI 评审，可以作为问题清单直接使用；
- 但它引用的 **benchmark 基线已过时**：`93 条 / MRR 0.6844` 是 `5b60fc4` 的口径，
  golden 集在 09-16~09-17 扩充后当前是 **105 条 / MRR 0.6677**。原评审 §3 关于
  P1-4 优先级的判断、§6 关于 Repair 开销收益的数字，都建立在当前**不可复现**的口径上；
- 它的 §7「四阶段 Evidence Runtime」在**当前部署规格（2 vCPU / 1.9 GiB 单机 VPS）**
  下是架构级重写，与仓库任务板既定顺序（TASK-109 → TASK-093 → TASK-110）冲突。

**采纳原则：按它做诊断，不按它做排期。**

> **附：本次复核还修掉一个阻断性回归**——`benches/param_sweep.py --verify` 已因
> 两个独立原因失效（用例集扩充 + provider 向量不可复现），详见 §1.2。

## 1. 基准口径漂移（本次复核最重要的发现）

原评审全篇引用 `benches/results/param-sensitivity-2026-09-15.md` 的
`(R@5 0.892 / R@10 0.925 / MRR 0.6844, n=93)`。该报告自称"实测于 `main @ 5b60fc4`"，
而在它之后 golden 集被三次提交扩充：

| 提交 | 日期 | langchain 用例数 |
|---|---|---:|
| `5b60fc4`（原评审口径） | 09-15 | 20 |
| `6836deb` | 09-16 | 26 |
| `7167388` | 09-17 | 29 |
| `83d877a` / `20346f8`（当前） | 09-17 | 32 |

其余三靶场（cockpit 38 / leveldb 20 / HelloAgents 20）未变，因此正例数
**93 → 105**（+12 条 langchain 调用链题）。

### 1.1 当前实测基线（本公司 WSL，持久索引复用，未重建）

设备：`company-wsl`（6 核 / 15.6 GiB WSL2）｜embedding：`api:voyage-4-lite@1024`
数据根：`~/.zace/bench/voyage-4-lite-d1024`｜预算：`maxTokens=10000`

| 靶场 | 正例数 | R@5 | R@10 | MRR | 负例 |
|---|---:|---:|---:|---:|---:|
| cockpit-agents-py | 36 | 0.861 | 0.861 | 0.562 | 2/2 |
| leveldb-v1 | 19 | **1.000** | **1.000** | 0.721 | 1/1 |
| helloagents-v1 | 19 | 0.842 | 0.947 | 0.766 | 1/1 |
| langchain-v1 | 31 | 0.871 | 0.968 | 0.703 | 1/1 |
| **合并** | **105** | **0.8857** | **0.9238** | **0.6677** | 5/5 |

对照原评审引用的 `93 条 / 0.892 / 0.925 / 0.6844`：

- R@5 与 R@10 基本持平（−0.006 / −0.001，在噪声内）；
- **MRR 下降 0.0167**，远超原报告自定的 `|ΔMRR| < 0.005` 噪声阈值。

原因是新增的 12 条 langchain 调用链题命中位次偏后——这批题正是
`7167388` / `83d877a` 两次修复针对的目标，说明**修好了召回但位次仍未达标**。

复现：

```bash
set -a; source ~/.config/zace/benchmark.env; set +a; export no_proxy='*'
for t in cockpit-agents-py leveldb-v1 helloagents-v1 langchain-v1; do
  uv run python benches/run.py --target $t --data ~/.zace/bench/voyage-4-lite-d1024 \
    --report benches/results/raw/runtime-review-$t.md
done
```

> **设备绑定**：上表只对 `company-wsl` 成立。原报告的数字是 `vps-la-2c2g` 口径，
> 两者不可直接比大小（`benches/results/README.md` §1 纪律）。

### 1.2 `param_sweep.py --verify` 已失效（两个独立原因）

`benches/param_sweep.py` 的 `OFFICIAL_BASELINE` 仍是 `(0.892, 0.925, 0.684, 93)`，
本机实跑报错：

```text
!! 离线复算与官方基线不符：得到 (0.8952, 0.9238, 0.6681)，期望 (0.892, 0.925, 0.684)。
   说明离线口径已漂移或索引/代码已变——本脚本的结论此时不可用。
```

即原评审 §6 引用的"75/93 触发、8 条 coverage 提升、开销/收益 6.4×"，
其产脚本在当前 main 上已不可运行。**这是一个必须先修的回归**（见 §3）。

失效有**两个独立原因**，只修用例数修不好：

**原因一：用例集扩充**（§1.1 已述），正例 93 → 105。

**原因二（更严重）：`api:voyage-4-lite` 的 query 向量本身不可逐位复现。**

`param_sweep` 的 `precompute()` 每次都真调 `provider.embed_query()`。实测：

| 观测 | 值 |
|---|---|
| 165 条 query 跨进程逐位相同的 | **65 条**（100 条不同） |
| 最大绝对差 | **5.6e-3** |
| 单条 query 连续 6 次（插入干扰查询） | 第 3 次出现 1.4e-3 偏差，其余为 0 |

抖动幅度足以让个别用例的排名掉一位。同一命令连跑四次：

| 次 | R@5 | MRR |
|---|---:|---:|
| 1 | 0.8952 | 0.6681 |
| 2 | 0.8952 | 0.6681 |
| 3 | 0.8857 | 0.6678 |
| 4 | 0.8857 | 0.6677 |

**R@5 摆幅 0.0095，而原地 `--verify` 容差是 0.002——容差比 provider 的实际噪底还紧。**
即使把基线数字改对，自检仍会间歇性假警报。

> 推论：原评审 §2 里 `vector_rank_top=0` 的信号从 −0.0334 衰减到 −0.0138，
> **至少有一部分是噪声而非真实衰减**（上面的四次里 R@5 就差了 3 条用例）。
> 该轴的衰减结论需带侧车缓存重测后才能采信。

**已修**（本分支）：

1. `precompute()` 接入 `PersistentQueryVectorCache`（仓库已有的侧车机制），
   默认路径 `<索引根>/query-vectors.json`，首次联网预热后完全离线；
2. 侧车绑定 `model_id` / `dim` 自证字段（否则文件里是 `null`，`identity_mismatch`
   退化为空操作，拿别的模型向量来跑无从察觉）；
3. `OFFICIAL_BASELINE` 更新为 `(0.8857, 0.9238, 0.6678, 105)`，并加了口径变更史注释；
4. 新增 `VERIFY_TOLERANCE = 0.012`（高于 provider 噪底）——侧车能锁住**同机**
   逐位一致，但换机器重新预热仍会落到噪声带另一头，容差不能低于噪底。

修复后三次连续 `--verify` 逐位一致，完整扫描端到端通过（含确定性自检）。

## 2. 关键轴在当前 105 条上的重测

绕过基线自检，在**当前用例集**上重跑原评审依赖的关键轴（同设备、同索引）：

| 轴 | 取值 | 旧值（93 条） | 现值（105 条） | 原结论是否成立 |
|---|---|---:|---:|---|
| Repair 关闭 | `gap_on=False` | −0.0109 | **−0.0113** | ✅ 成立且更显著 |
| 基准分缩放 | `×100` | −0.0209 | −0.0202 | ✅ 成立 |
| 基准分缩放 | `×10` | −0.0090 | −0.0083 | ✅ 成立 |
| 测试抑制 | `test_fixture=0` | −0.0182 | −0.0142 | ✅ 定向成立，幅度缩水 22% |
| 向量语义特征 | `vector_rank_top=0` | **−0.0334** | **−0.0139** | ⚠️ **幅度掉到 42%，但见 §1.2：至少有部分是 provider 噪声，需带侧车缓存重测** |
| `seeds` | 10 / 40 | ±0.0000 | ±0.0000 | ✅ 成立 |
| `max_expanded` | 15 / 60 | ±0.0000 | +0.0000 / +0.0010 | ✅ 成立 |
| `tier3_ratio` | 0.45 | ±0.0000 | ±0.0000 | ✅ 成立 |
| `single_file_ratio` | 0.40 | ±0.0000 | ±0.0000 | ✅ 成立 |
| `score_ratio` | 0.15 | +0.0021 | +0.0023 | ✅ 仍在噪声内 |
| `docs_ratio` | 0.05 | +0.0035 | ±0.0000 | ✅ 仍在噪声内 |

两点值得记下：

1. **原评审 §6「Repair 不能删」的判断在当前口径上更强**（−0.0113），
   它建议"保留 I1-I3 三条不变量"的结论可以继续用；
2. **`vector_rank_top` 的信号看似大幅衰减**（−0.0334 → −0.0139），但 §1.2 证明
   provider 自身有 0.0095 的 R@5 噪底，**这个结论现在不能采信**。
   本分支已给 `param_sweep` 接入侧车缓存；**带缓存重测该轴之后才能下结论**。

## 3. 逐条采纳判断

> **状态列于 2026-09-17 实现后更新**：下表前 5 行已在本分支落地（P1-2 / P2-1 / P2-5 /
> P1-1 / P1-5），其余未动。实现细节与验证见 §5。

| 原评审条目 | 判断 | 状态 | 理由 |
|---|---|---|---|
| **P1-2** 删除文件可靠清理向量 | **立即做** | ✅ 已实现 | 已承认缺陷、改动小（`Store` 加批量接口）、不触碰检索语义 → **跑分零影响** |
| **P2-1** query cache 提到 Engine 生命周期 + 引入 model 身份 | **立即做** | ✅ 已实现 | 事实核实：service 路径 `Engine.open()` 未注入 cache，60s 复用**确实不存在**；单次查询结果不变 → **跑分零影响** |
| **P2-5** `files.generated` 恒为 0 | **立即做** | ✅ 已实现 | `store.py` 硬编码 `0` 属实；现落库（文件名约定 + 内容 banner）→ **本索引上零漂移，但发现并修掉一个误报**（见 §5.3） |
| **P1-1** 索引非原子提交 | **做简版** | ✅ 已实现 | 认问题，**不认 generation 双目录**：`~/.zace/bench/` 下已有 7 个持久索引（langchain 179 MB），改目录布局等于全部重建。已落它自己给的备选方案 `index-state.json` |
| **P1-5** 查询可能读到中间态 | **做简版** | ✅ 已实现 | 认同风险，但**不引入 ProjectActor**：2 vCPU 上常驻资源池是过度设计。现已让 search trace 暴露该状态 |
| **P1-3** 向量超时线程不可取消 | **降为 P2** | 未做 | 事实属实，但原评审低估了兜底：`api.py` 已有 `httpx` `connect=10s / total=60s`，通道超时 5s，堆积窗口有上限，非"无限积累"。先补 deadline 透传 |
| **P1-4** 并行召回 + 四分支轻路由 | **认同低优先级** | 未做 | 原评审 §3 已自行标注可接受；且其依据的跑分口径已漂移，须先重测 |
| **P2-2** `reasons` 字符串当控制协议 | **推迟** | 未做 | 判断正确（`rerank.py` / `engine.py` 属实），但 sidecar 改造动 `Candidate` 消费面，属重构 |
| **P2-3** 候选对象可变污染 | **推迟** | 未做 | 现象已被 `param-sweep` §2 独立发现并留档，无新增信息 |
| **P2-4** 图扩展 N+1 SQL | **推迟** | 未做 | 当前池约 120，未成为实测瓶颈 |
| **P2-6** 拆 assembly + 边际效用装填 | **不采纳** | — | 与 `R29/R30` 冻结纪律及"4 类参数敏感度为 0"的实测冲突；且原报告自述"必须离线 replay 对比，不直接替换" |
| **P2-7** Answer 归属 core/service | **转契约讨论** | 未做 | 判断正确：`interfaces.py` 与 `Module/06 §5` 说 `llm/` 属 core，实际 `core/zace_core/llm/` **不存在**，全在 `service/zace_service/answer.py`。但这要走契约变更流程，**不自行改实现** |
| **§6-3** 删除 `chain_priority` 越界启发式 | **暂不删** | 未做 | 设计文档已标注"建议删除"，但它现在是 `engine.py` 排序键的首位成分，**且为 09-17 两次修复留下**。删除前必须 replay A/B |
| **§7** 四阶段 Evidence Runtime | **不整体采纳** | — | 见 §4 |

## 4. 为什么不采纳 §7 的"Evidence Runtime 为主轴"

原评审 §7 的四阶段合计涉及：generation staging + 双目录迁移、`ProjectActor` 常驻
资源与单写队列、`QueryFacts/QueryPlan/ChannelResult/ExecutionTrace` 内部类型体系、
批量 `EvidenceSnapshot`、Candidate 不可变化、Evidence Ledger、Rank/Pack 离线 replay、
`assembly.py` 拆四模块。**这已接近一次架构级重写**，而它自己在 §8 写着
"不直接 Rust 重写整个 core"——两处口径不一致。

约束上的冲突有两处：

1. **部署规格**：生产是 `2 vCPU / 1.9 GiB` 单机 VPS（`benches/results/index-cost-model-vps.md`）。
   `ProjectActor` 的"常驻项目资源 + warm VectorStore + 固定容量 bulkhead"在该规格上
   会与索引任务争内存（索引期峰值 RSS 已达 928 MB / langchain）。
2. **仓库既定顺序**：`docs/tasks/README.md` 明确下一步是
   **TASK-109（检索质量）→ TASK-093（真实数据闭环）→ TASK-110**；
   `param-sweep` §5 也写"先做 TASK-093 的真实数据闭环"。
   在真实数据闭环之前启动 runtime 重写，等于**在没有可信分布的情况下重构**。

**可提取的部分**：原评审 §5.5 的 Evidence Ledger 思路本身有价值（让 `reasons` 退化为
可读投影），但它不该作为独立基建先行，而应作为 TASK-093 数据闭环的**采集字段**顺带落地。

## 5. 建议的下一步（按性价比排序）

1. **修 `param_sweep.py` 的基线自检（✅ 已完成）**——接侧车缓存 + 更新
   `OFFICIAL_BASELINE` 到 `(0.8857, 0.9238, 0.6678, 105)` + 放宽到
   `VERIFY_TOLERANCE = 0.012`。**这是当前最高优先级**：不修，任何 ablation 都不可运行，
   也察觉不到 §2 里 `vector_rank_top` 那种信号变化（真假莫辨）。
2. **带侧车缓存复查 `vector_rank_top`**——现有 −0.0139 里至少部分是噪声，
   须在锁定的向量上重测才能判断语义特征是否真的失效。
3. **P1-2 / P2-1 / P2-5 三项止血（✅ 已完成，见 §5）**。
4. **P1-1 / P1-5 简版（✅ 已完成，见 §5）**。
5. **其余条目挂到 TASK-093 之后**，作为真实数据驱动的评估输入，而不是直接排期。
6. **P2-7 开契约讨论**（Answer 归属），走 §4 流程，不在本轮改实现。

## 6. 本轮实现与验证（2026-09-17）

### 6.1 改动清单

| 文件 | 改动 | 对应条目 |
|---|---|---|
| `storage/store.py` | `apply_deletions` 返回被删 chunk id；新增 `generated_files()`；`apply_file_change` 接 `generated` 参数并写入真实值 | P1-2 / P2-5 |
| `pipeline/indexer.py` | 整文件删除改由 `Store` 返回 id 清理向量；索引期传入 `is_generated()` | P1-2 / P2-5 |
| `pipeline/generated.py` | **新增**：generated 判定（文件名约定 + 内容 banner） | P2-5 |
| `pipeline/index_state.py` | **新增**：`index-state.json` 读写与对账 | P1-1 / P1-5 |
| `engine.py` | index-state 构建/就绪/失败标记与查询降级合并；query cache 生命周期 + 模型身份绑定 | P1-1 / P1-5 / P2-1 |
| `retrieval/vector.py` | `QueryEmbeddingCache` key 加入模型身份；`bind_identity()` 换模型即清空 | P2-1 |
| `retrieval/rerank.py` | generated 信号改为读真实列 **∪** 文件名约定 | P2-5 |
| 测试 | 新增 `test_index_state.py`、`test_query_cache_lifecycle.py`；扩充 indexer / vector / cascade 测试 | 全部 |

### 6.2 跑分影响（实测）

四个靶场、105 正例，改动前后**逐位零漂移**：

| 靶场 | 正例 | R@5 | R@10 | MRR |
|---|---:|---:|---:|---:|
| cockpit-agents-py | 36 | 0.861 | 0.861 | 0.562 |
| leveldb-v1 | 19 | 1.000 | 1.000 | 0.721 |
| helloagents-v1 | 19 | 0.842 | 0.947 | 0.766 |
| langchain-v1 | 31 | 0.871 | 0.968 | 0.703 |
| **合并** | **105** | **0.8857** | **0.9238** | **0.6678** |

零漂移是**预期结果**而非“没生效”：现有持久索引建于改动之前，`files.generated` 全为 0，
而 rerank 取的是“真实列 **∪** 文件名约定”的并集 → 旧索引行为**必须**不变。
要看到 P2-5 的增益必须**重建索引**。

### 6.3 P2-5 的一个真实误报（本次对照的主要收获）

首版 banner 关键词含裸 `"generated by"`，在真实靶场上命中：

- **`leveldb/table/block.cc`**——注释写着 "Decodes the blocks **generated by**
  block_builder.cc"。这是 leveldb 的**核心手写文件**，误降权会直接伤害真实证据；
- 另有 langchain 的 10 个 `_profiles.py` 是**真**生成文件（带 "Auto-generated / DO NOT EDIT"），
  属正确识别。

已收紧关键词（去掉裸 `"generated by"`，保留 `code generated by` / `automatically generated`
等强标记），并加回归测试锁住这个边界。收尾后的识别结果（内容 banner 增量）：

| 靶场 | 文件数 | 仅文件名可识别 | 仅内容 banner 可识别 |
|---|---:|---:|---:|
| cockpit | 287 | 0 | 3（`*_pb2.py` / `*_pb2_grpc.py`） |
| leveldb | 152 | 0 | **0**（误报已消除） |
| HelloAgents | 236 | 0 | 0 |
| langchain | 2950 | 0 | 9（`data/_profiles.py`） |

**12 个文件的识别与 golden `expected` 交集为 0**——即本改动在当前靶场上不影响任何用例的
命中集合。

### 6.4 真实验证（不带 mock）

用隔离数据根 + 4 个小 C++ 文件验证端到端（`/tmp/gen-probe/`）：

```text
files.generated: 1 ← schema.pb.cc        （文件名约定）
                 1 ← banner_only.cc     （内容 banner，旧代理做不到）
                 0 ← handwritten.cc / plain.cc

index-state.json: {"status": "ready", "expected_chunks": 6, "expected_vectors": 6}

删除一个文件后：chunks=5  vectors=5  一致=True
                （旧实现此处会留下孤儿向量——跨实例删除必现）
```

### 6.5 回归检查

```text
uv run ruff check .                        → All checks passed!
uv run python scripts/check_dependency_direction.py → 依赖方向检查通过
uv run pytest core/tests/ service/tests/    → 1268 passed, 9 skipped
```

## 7. 复核方法可复现性

- 代码事实：逐条 `sed` 读取原评审给出的行号区域，比对行为描述；
- 跑分：`benches/run.py` 逐靶场 eval（复用持久索引，未 ingest）；
- 轴扫描：`benches/param_sweep.py`（已接侧车缓存，可重跑）；§2 的早期数字
  由临时脚本绕过旧自检得出，逻辑复用其 `precompute` / `evaluate`，不外发数据；
- 设备：`company-wsl`，与 §1.1 表头一致。

## 8. 未决问题

- 原评审 §P1-4 标题内嵌了 `【但是目前总体2秒左右，可以接收，低优先级，无所谓】`
  的人工批注，与 AI 评审正文混在同一段。**建议后续把人工批注与机器评审分栏**，
  否则引用时无法判断哪句是评审结论、哪句是人工判断。
- 本复核只在 `company-wsl` 完成，**未在 `vps-la-2c2g` 复现**（该机才有原报告的设备口径）。
  若要判定"原报告数字是否曾成立"，需上 VPS 跑一次。
- §1.2 的 provider 抖动只确认了**现象**（量化了幅度与概率），**未定位根因**：
  可能是上游批处理/MoE 路由的浮点归约顺序，也可能是网关侧。仓库做法应保持
  "以侧车缓存为准"，不依赖 provider 逐位可复现。
- 侧车文件当前落在 `~/.zace/bench/voyage-4-lite-d1024/query-vectors.json`（110 条）。
  **是否纳入 `benches/results/raw/` 版本化分发**（像 `raw-task109-*.md` 那样跨机复现）
  需编排者定：纳入则任何机器都能跑出逐位一致的数，不纳入则换机器仍会落到噪声带另一头。
- P2-5 的内容 banner 列表是**保守起点**（只能识别强标记）。设计文档 §6 待决项 2 还提到
  “C/C++ 的 build 产物目录（build/、cmake-build-*）默认忽略”——那是**扫描侧**规则
  （``pipeline/ignore.py``），不在本次改动范围内，未动。
- P1-1 的 ``index-state.json`` 只能**发现**中间态并降级，**不能防止**它发生（真正的原子性
  仍需 generation staging）。当前实现在同一进程内串行索引时不会产生中间态窗口；
  跨进程并发索引仍是“最后一个写完者胜”。
