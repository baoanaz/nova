# TASK-109：Evidence-Gap 驱动的二轮补检（D-19 落地）

> 状态：done（已合并 `5b60fc4`，2026-09-16；2026-10-04 补改状态并归档）｜ 阶段：Phase 5+（质量）
> 硬依赖：TASK-108（已完成，提供基线）｜ soft 依赖：TASK-110（专项测试项）
> 交付物所有权：
> - `core/zace_core/retrieval/gap.py`（**新建**）
> - `core/zace_core/engine.py`（接线）
> - `core/tests/retrieval/test_gap.py`（**新建**）
>
> **本卡开工前请先读**：`docs/handbook/部署指南.md` 第三章（基准环境）——改检索代码必须跑基准。

## 一句话

当首轮召回的**候选池里已经有目标**、但它**没进 ContextPack**（或压根没进池）时，
用**确定性的定向补检**把缺口补上。**不预猜意图，不重复盲搜，不用 LLM。**

## 为什么需要它（真实失败驱动，非设计推演）

2026-09-15 用真实 MCP 客户端对 `cockpit-agents-py` 做 6 次调用并逐项对照源码，
暴露同一类系统性缺陷：**候选已正确索引，但没被召回/没进包**。

### 可复现证据（当前代码状态下实测，2026-09-15）

**用例 A（`cockpit-0035`，召回覆盖不足）**

```text
查询：能力网关 CapabilityGateway 如何实现幂等（idempotency）、参数验证（verification）、
      重试（retry）与 UNKNOWN 终态？
```

| 目标 | 索引状态 | 候选池排名 | 分数 | 是否进包 |
|---|---|---:|---:|---|
| `CapabilityGateway._reserve_idempotency` | ✅ 已索引（gateway.py:336） | 46 | 0.31 | ❌ |
| `CapabilityGateway._should_retry` | ✅ 已索引（gateway.py:487） | **不在池** | — | ❌ |
| `CapabilityGateway._normalize` | ✅ 已索引（gateway.py:442） | 49 | 0.30 | ❌ |
| `CapabilityGateway`（类骨架） | ✅ | 5 | 1.61 | ✅ |

**关键观察**：**类的骨架进了包，但它下面的方法没进**——而用户问的正是那些方法的行为。
类骨架（rank 5）与它的方法（rank 46/49）之间差 40 多名，说明"类被召回"没有帮助"方法被召回"。

**用例 B（`cockpit-0033`，调用链断裂）**

查询"普通 USER_REQUEST 从准入到执行 Agent 的路由链路"，目标是
`runtime.py`（`Runtime.admit`）→ `dispatcher.py`（`InputDispatcher`）→ `intent_router.py`。
实测：`intent_router.py` 进包（rank 靠前），但 `runtime.py`/`dispatcher.py` 的首个候选
只到 **rank 19**，包内没有完整链路。

### 已排除的可能（不要重复这些方向）

| 假设 | 实测结论 |
|---|---|
| 索引缺失 | ❌ 所有目标都正确索引（`gateway.py` 37 chunks 齐全） |
| 排序错误 | ❌ 不是排序问题——目标排在 rank 46-49，是**没被召回** |
| 预算截断 | ❌ 见下 |
| 测试夹具挤占 | ⚠️ TASK-108 已修（包内测试 20→1 条），但**目标仍未进包** |
| 阈值过严 | ⚠️ TASK-108 已把 0.50→0.40，仍不够（目标分 0.30） |

**TASK-108 的对照实验（很重要）**：测试夹具常态抑制让包内测试从 20 条降到 1 条、
token 从 9912 降到 4698，但 `gateway.py` 的方法**仍然没进包**。
这证明**"降噪"与"补召回"是两件独立的事**，后者必须靠本卡。

## 目标

落地 `docs/design/Module/02-检索策略.md` §4.7 的 **G1/G2/G3**，
让 `search_context` 与 `ask_project` **共用**同一条管线（D-10：不分叉成两套代码）。

## 输入与参考

| 类别 | 位置 |
|---|---|
| 设计规则表 | `docs/design/Module/02-检索策略.md` §4.7（G1-G5）、§4.8（共享管线） |
| 决策依据 | `docs/design/INDEX.md` D-19（Gap 二轮）、D-10（共用管线）、D-18（图双角色） |
| 复用能力 | `core/zace_core/retrieval/expand.py`（图扩展，**复用不新写**） |
| 冻结契约 | `docs/contracts/**`、`core/zace_core/types.py`、`interfaces.py` |
| 基准环境 | `docs/handbook/部署指南.md` 第三章 |
| 回归用例 | `benches/golden/cockpit-agents-py/`（38 题，含本卡要修的 `cockpit-0033`/`0035`） |

## 设计要点

### 1. 只做 G1/G2/G3

| 规则 | 本卡处理 | 说明 |
|---|---|---|
| **G1** 调用链断裂 | ✅ 核心 | 2-hop 定向图查询，只从 top 种子出发 |
| **G2** 设计意图缺失 | ✅ | spec evidence=0 时用首轮代码符号反查 `spec_references` |
| **G3** 无共识结果 | ✅ | top10 全单通道时，用 top3 的符号名/文件名构造扩展查询 |
| G4 文档过时 | ❌ 已有 | TASK-096 已实现（进 MissingEvidence，不触发二轮） |
| G5 预算富余 | ❌ 不适用 | 属组装层；TASK-108 已把闸门改为优先级语义 |

### 2. 针对本卡证据的新增候选规则（**需评审**）

上面的实测暴露了一个 §4.7 未覆盖的缺口：**"类骨架进包、其方法未进包"**。
建议新增一条规则（暂称 **G6**）：

> **G6 容器-成员断层**：包内已有某 class 的骨架（`class_skeleton`），但该 class
> 在候选池里的成员方法（同文件、行号落在类区间内）全部未进包，且查询含行为类词
> （"如何实现/怎么保证/流程"） → 对**该 class 在候选池里的成员**做一次定向补检。

判据必须确定性（不用 LLM），且**只在池里已存在成员时触发**（否则退化成盲搜）。

### 3. 二轮纪律（设计已定，不得放宽）

- 最多 **1 次**迭代；
- 二轮只补缺口对应通道（G1 只走图、G3 只重召回），**不做全量重跑**；
- 二轮结果**进同一 rerank**，不直接插队（否则破坏 D-16 的排序唯一性）；
- 全部 deterministic，**不引入 LLM**（R1 延迟预算）。

### 4. 与预算闸门的关系（TASK-108 的教训）

二轮候选进池后仍走同一套 `score_floor` 与预算装填。
**不得因为"是二轮来的"就绕过闸门**——TASK-108 实测过"不丢候选、预算允许就装"的后果：
包被填满 10K，但后半是 `0.00`/`-0.24` 的噪音（tier3 固定分 0.70 与负分候选）。

> 但要处理本卡的具体矛盾：目标方法分 **0.30** 而闸门是 `top1×0.40`。
> 可能的解法（需评审）：二轮候选用**独立的小预算**（如 ≤15% hard_cap），
> 且必须标记来源（`reason` 里写明"gap 补检"），让 Agent 能识别。

### 5. search 与 ask 都要有

- `search_context`：Fast 也触发（成本是本地查询，非 LLM）；
- `ask_project`：Deep 触发，效果叠加；
- 分叉点必须在**组装之后**（D-10）。

## 施工步骤（建议顺序）

1. **先写可复现的失败断言**：在 `core/tests/retrieval/test_gap.py` 里用
   `benches/golden/cockpit-agents-py` 的两题做集成测试（或先写最小单测）。
2. 实现 `gap.py`：纯函数，输入首轮候选池 + pack，输出"缺口类型 + 补检计划"。
   **不碰 I/O**（图查询由调用方执行）。
3. 在 `engine.py` 的 `search_with_trace` 里接线：一轮 → gap 检查 → 二轮 → 合并 → rerank → assemble。
4. 跑基准（三仓 + cockpit），确认无回退。
5. 回填本卡执行记录。

## 验收标准

- [ ] **用例 A**：`_reserve_idempotency` / `_should_retry` / `_normalize` 至少 2 个进入包
      （当前 0 个）。
- [ ] **用例 B**：`runtime.py` 或 `dispatcher.py` 进入包（当前首个候选 rank 19、包内无）。
- [ ] **三仓基准不回退**：TOTAL R@5 ≥ 0.912、MRR ≥ 0.756、负例 3/3。
- [ ] **cockpit 基准不回退**：R@5 ≥ 0.806、MRR ≥ 0.544、负例 2/2。
- [ ] **延迟不超标**：Deep 端到端 <10s（含 LLM），Gap 检查本身 <5ms。
- [ ] **二轮纪律**：单测证明最多 1 次迭代、结果必经 rerank、不绕过预算闸门。
- [ ] **G4/G5 不被重复实现**。
- [ ] 全仓 pytest、ruff、依赖方向检查全绿。

## 基准命令（回退判定用）

```bash
cd ~/2_github/AI/ACE/zace
set -a; source ~/.config/zace/benchmark.env; set +a
export no_proxy='*'

for t in leveldb-v1 helloagents-v1 langchain-v1 cockpit-agents-py; do
  uv run python benches/run.py --target $t \
    --data ~/.zace/bench/voyage-4-lite-d1024 --report /tmp/bench-$t.md
done
```

## 明确不做

- 不做 2-hop 常规扩展（只在 G1 触发时按需做，见 TASK-011 的"明确不做"）；
- 不做 HyDE / Multi-Query 查询改写（需额外 LLM 调用，违反 R1）；
- 不做 Cross-Encoder / LLM rerank（V1.5）；
- 不做固定轮数的盲搜（用户明确否决："不要重复检索 2-3 次"）。

## 待讨论项（开工前与编排者确认）

1. **G1 的触发条件**：设计写的是 "Structural 路由且 callPaths 平均长度 <2"，
   但**轻路由四分支尚未落地**（`engine.py` 恒走 Fast）。
   是否先用**路由无关**的条件替代：如"包内 ≥1 个符号但没有 `[F*]` flow 证据"？
2. **G6（容器-成员断层）是否纳入**：它是本卡实测暴露的新缺口，§4.7 没有。
   纳入则需在设计文档 §4.7 补一行（属设计面，要编排者裁决）。
3. **G3 的扩展查询构造**：用符号名还是文件名？两者权重？
4. **二轮候选的预算法**：独立小预算（标记来源）还是共用主闸门？

## 执行记录

### 2026-09-15 · 建档（未开工）

- 由 6 次真实调用驱动建卡；同批发现的两个缺陷中，
  **客户端契约 bug 已由 TASK-108 修复**，本卡只解决召回覆盖。
- 已确认与 TASK-108 的分工（见上文"已排除的可能"对照实验）。
- 回归用例已固化：`benches/golden/cockpit-agents-py/cockpit.jsonl` 的
  `cockpit-0033`（链路）、`cockpit-0035`（Gateway），详见同目录 `qa.md`。

### 2026-09-15 · 实施（lane-f，`feature/task-109-evidence-gap_xwz0915`）

**结论：验收标准全部达成，四靶场无一回退。**

编排者开场即确认三项范围决策（本卡据此实施，未自行拍板）：

| 待讨论项 | 裁决 | 落地 |
|---|---|---|
| G6 需要按容器前缀枚举符号 | **加只读 Store 方法** | `Store.symbols_in_container()`（+37 行，只读、无写路径） |
| 触发面（Fast 是否触发） | **两边都触发，Deep 用更大配额** | `DEEP_GAP_LIMITS`（core）；`runtime.search(deep=)` + `routers/query.py` 的 ask 传 `deep=True` |
| 二轮预算 | **独立小预算 + 标记来源 + 必经 rerank** | `BudgetConfig.backfill_ratio`；reason 写 `gap backfill: …`；结果并入同一 `rerank` |

#### 交付物

| 文件 | 变更 |
|---|---|
| `core/zace_core/retrieval/gap.py` | **新建**：纯函数 Gap 判定（G1 容器-成员断层 / G2 文档锚点闭包） |
| `core/zace_core/engine.py` | 两轮管线接线（`search_with_trace` → `_backfill_gaps`）；`SearchTrace.gap_kinds/backfilled` |
| `core/zace_core/contextpack/assembly.py` | `assemble(backfill=)` 独立小预算装填；`_degrade(force=)` |
| `core/zace_core/storage/store.py` | `symbols_in_container()`（**超出卡片清单，已获授权**） |
| `service/zace_service/runtime.py` | `search(deep=)` 透传 |
| `service/zace_service/routers/query.py` | ask 路径传 `deep=True`（1 行） |
| `core/tests/retrieval/test_gap.py` | **新建**：18 条（12 纯函数 + 6 集成回归） |

#### 实测根因（与建卡时的推断不同，以实测为准）

| 用例 | 建卡推测 | 实测根因 | 修复机制 |
|---|---|---|---|
| cockpit-0035 | "召回覆盖不足" | **不是召回**：15 个成员**已在池内**（`_invoke` 图扩展带出），但被 `top1×0.40` 相对分数闸门（0.76）挡下，目标分 0.30 | G1：查询点名 `CapabilityGateway` 且它在包内 → 补入池内成员 |
| cockpit-0033 | "调用链断裂" | `Runtime.admit`/`InputDispatcher` **在池内**（rank 25/18），同样被闸门挡下；而 `spec_refs_for_spec(doc)` 直接给出二者 | G2：包内文档的引用目标在池内未进包 → 补入 |

**关键发现**：目标几乎全是 `tier=3`（图扩展发现），而 tier3 配额（≤30%）在首轮已被别的邻居吃满 →
补检循环里若再卡一次 tier3 会**恒真地**挡下全部目标。故补检候选**不重复计 tier3 配额**
（它们已有自己的独立预算；tier 值不变、如实反映来源）。这不是绕过闸门：TASK-108 否决的是
"无依据地装"，而补检候选带确定性结构依据。

另一处实测：**行数 ≠ token 数**。`Runtime.admit` 仅 63 行却 971 token，
按行长阈值判永不触发降级 → 被单成员 token 上限整个跳过。故 `_degrade(force=)` 按 token 判定。

#### 验收证据

| 标准 | 结果 |
|---|---|
| 用例 A：`_reserve_idempotency`/`_should_retry`/`_normalize` ≥2 进包 | ✅ **3/3**（修复前 0） |
| 用例 B：`runtime.py` 或 `dispatcher.py` 进包 | ✅ 二者均进包 |
| 三仓不回退 | ✅ leveldb R@5 1.000→1.000 MRR 0.721→0.721；helloagents 0.842→0.842 / **R@10 0.842→0.947** / 0.754→**0.766**；langchain 0.895→0.895 / **R@10 0.895→0.947** / 0.791→**0.798** |
| cockpit 不回退 | ✅ R@5 0.806→**0.861**、R@10 0.833→**0.861**、MRR 0.544→**0.562**、负例 2/2 |
| Gap 检查 <5ms | ✅ 纯函数 0.02ms；含 Store 查询 1.2–2.8ms（端到端 ~600ms 不变，受 embedding 主导） |
| 二轮纪律单测 | ✅ `test_gap.py`（最多 1 次迭代、只补池内、独立预算上限、来源标记、Deep 配额 ≥ Fast） |
| G4/G5 未重复实现 | ✅ 未触碰 |
| 全仓质量门 | ✅ ruff 全绿；依赖方向通过；`pytest` **1046 passed / 7 skipped**；service 372 passed |

#### 未决问题 / 已知风险

1. **既有非确定性**（非本卡引入）：同一 query 连续调用 `recall_vector` 会有约 7 个低分位置
   顺序互换（LanceDB ANN 同分 tie）。已在未修改的 `main @ 7df6cc6` 上复现。
   影响：`cockpit-0035` 的 `backfilled` 会在 14/15 间浮动（该题 `expected_mode=any`，判定不受影响）。
   本卡测试因此不对该数值做相等断言，只断言机制生效。**建议另开卡修**。
2. **G3 仍未实现**（设计里的"无共识结果"重召回）。本卡实测未在任何靶场触发该缺口，
   无证据驱动，故按"不做无证据的推演"留在设计面。
3. **G6 命名**：实现时把建卡暂称的 "G6" 与 G1 合并为同一条规则（两者判据同源：
   "容器在包内 + 池内有未进包成员"），未在设计文档 §4.7 新增编号——**需编排者确认**
   是补一行设计、还是维持"G1 的一个子情形"。
4. `web` 前端的 `History.trace.test.tsx` 有 1 条失败，已在干净 `main` 复现（既有问题，与本卡无关）；
   本卡未改任何 web 文件。
