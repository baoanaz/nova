# TASK-023：真实场景用例采集（检索质量优化的唯一依据）

> 状态：pending（**由 TASK-093 落地，不单独开工**）｜ 阶段：**Phase 2**（与 service/client 同期建设）｜ 硬依赖：TASK-040（service 骨架，提供落库位置）
> 建议分支：`feature/task-023_<你的缩写><MMDD>`
> 交付物所有权：`service/zace_service/telemetry/`（新建）、`core/zace_core/contextpack/assembly.py`（仅追加审计字段，不改判定逻辑）、
> `docs/plan/real-eval.md`（新建，采集口径与隐私边界）、`benches/real/`（采集产物，**.gitignore 排除**）

## 目标（用户 2026-09-10 拍板 R29/R32）

把检索质量的优化依据，从"编排者猜的 60 条"换成"**真实 AI 在真实任务里实际发出的查询**"。

> 用户原话要点：真实场景下 query 是**另一个 AI 生成的**，问题形态可能是"用户的需求有没有参考"
> "为什么这么设计"这类奇怪问题；我们必须看到 **AI 会用什么提示词作为参数传入 Tool**。

**这是 R21/R24/TASK-015-B 等一切质量调优的前置**：在这些数据到位前，质量参数不再调整（R30）。

## 为什么不能靠现有 golden set

| 维度 | 现有 golden（60 条） | 真实采集 |
|---|---|---|
| query 作者 | 编排者（懂实现细节） | Coding Agent（只看用户需求） |
| 期望答案 | 出题人指定路径/符号，含主观假设 | 无法预先指定；靠"是否解决了任务"反推 |
| 分布 | 人工平衡（语言/类型） | 真实偏斜（就是偏斜才真实） |
| 用途 | smoke + 回归护栏（R29） | **优化目标** |

## 交付内容

### A. 采集层（服务端埋点）

- 每次 `search_context` / `ask_project` 落一条审计记录（Module/04 §8 已有此设计，本卡扩展字段）：
  ```text
  { requestId, ts, tool: search|ask, query, mode,
    // 检索侧（不含源码内容）
    channelsUsed[], degraded, candidateCount,
    evidence: [{id, path, lines, kind, tier, score, reasons}],   // 路径与行号保留，正文不存
    // 消费侧（这是本卡新增的关键字段）
    confidence, answerable, missingEvidenceCodes[],
    // 反馈信号（见 §B）
    followupToolCalls, taskOutcome, userRating }
  ```
- **隐私边界（写进 `docs/plan/real-eval.md` 并在设置页明示）**：
  只存 **query 文本 + 证据的 path/行号/分数**，**绝不存源码正文、不存 AI 的完整回答**；
  路径本身可能含敏感目录名 → 提供"采集开关 + 路径脱敏（可选 hash 前缀）"配置。
- 落库位置与保留策略由 service 决定（per-project、最近 N 条、可一键清空）。

### B. 反馈信号（没有反馈的数据无法作为优化目标）

至少实现前两项，其余列为可选：

1. **后续工具调用链**（自动，最有价值）：一次 `search_context` 返回后，AI 在 M 分钟内还调用了
   哪些工具（grep/read/再搜索）？——**如果 AI 拿到上下文后仍大量 grep/read，说明这次检索没解决问题**。
   MCP 是 stdio 本进程，client 可自然记录；服务端通过 client 上报聚合。
2. **重复查询检测**（自动）：同一会话内相似 query 反复出现（改写重问）→ 说明首次检索失败。
3. （可选）**显式评分**：你在 WebUI/CLI 上对某次结果点赞/点踩。
4. （可选）**任务结果标注**：事后给某个真实任务打"最终解决/未解决"标签。

### C. 从日志到用例集

- `benches/real/build_cases.py`：从采集记录生成候选用例，分组呈现：
  - **高价值正例**：后续无 grep/read 且任务成功 → 这批是"zace 确实帮上忙"的证据；
  - **疑似失败**：后续大量 grep/read，或重复提问 → 这批是优化靶子（**比人工出的负例有效得多**）；
  - **AI 措辞样本**：直接导出 query 文本分布（长度/语言/是否含符号/是否含提问句式），
    用于理解"AI 怎么把用户需求翻译成 query"。
- 产物落 `benches/real/`，**不进 git**（含真实业务路径，加进 `.gitignore`）；
  可提交的只有聚合统计与脱敏样本（由你人工挑选）。

## 验收标准（DoD）

- [ ] 采集层：unit test 覆盖"记录写入/字段完备/开关关闭时不写/路径脱敏"；
- [ ] 「后续工具调用链」信号在 client 侧有实现路径说明（本卡可先只做服务端字段占位 + 设计文档，
      实现在 TASK-042 后补）；
- [ ] `build_cases.py` 能对一份人工构造的样例采集记录输出三类分组（用 fixture 测试，不依赖真实数据）；
- [ ] `docs/plan/real-eval.md` 写清：采集什么/不采集什么/隐私边界/保留与删除/如何生成用例；
- [ ] 全仓 `uv run pytest` 全绿；`uv run ruff check .`、`check_dependency_direction.py` 通过。

## 明确不做

- 不采集源码正文、不采集 AI 回答全文（隐私红线）；
- 不自动把采集数据当 golden（需要你/编排者筛选，防止把噪声当目标）；
- 不基于本卡数据调参（那是后续独立的校准卡，且必须有统计显著性）。

## 参考

- `docs/design/Module/04-AI总结.md` §8（审计存档的既有设计，本卡在其上扩展）
- `docs/design/Module/06-服务化与部署.md` §2.4（审计归属 service）
- `docs/contracts/PROCESS.md` §3.6 R29-R32（本卡的立项依据与纪律）

## 完成报告（回填）

填写：分支 / 验收命令与结果 / 契约影响 / 与设计偏差 / 未决问题。

## 执行记录

（实施 AI 在此填写。）
