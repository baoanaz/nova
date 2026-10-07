# Benchmark 方案

[文档中心](../../README.md) · [公共靶场与 runner](../../../benches/README.md) · [历史报告](../../../benches/results/README.md)

zace 的评测分别回答三个问题：**找得准不准、索引与检索有多快、最终回答有没有依据**。三类结果分开报告，并绑定输入版本、模型、配置和硬件。

## 1. 评测层次与现有工具

| 层次 | 目标 | 当前入口 | 外部调用 |
|---|---|---|---|
| 检索质量 | recall、排名、负例、证据覆盖 | `benches/run.py` + `benches/golden/` | 建索引及未缓存的查询向量可能调用 embedding API |
| 本地处理性能 | 分离解析、切片、准备与入库的本机开销 | `scripts/benchmark-vps.sh local` | 无真实 API 请求，不加载 ONNX 模型 |
| 机器与网络 | CPU 和到指定端点的吞吐 | `scripts/benchmark-vps.sh hardware` | 访问公开测速端点，默认约 540 MB 流量 |
| 真实 API 性能 | 首响应、吞吐、完整冷启动 | `scripts/benchmark-vps.sh ttfb/full` | 使用真实 embedding 凭据并消耗配额 |
| LLM 问答质量 | 回答正确性、引用和证据完整度 | 固定问题与参考答案，结合现有 QA 探针和人工复核 | 实际 ask 调用可能消耗 LLM 配额 |

现有自动化质量指标以 core eval 为准。下面的 LLM 问答流程是评测约定，不能把它写成已经覆盖全部场景的自动评分系统。

## 2. 固定输入与配置

默认公共质量回归包括 `leveldb-v1`、`helloagents-v1`、`langchain-v1`，另有教程仓 `hello-agents` 与 `zace` 自检。清单以 [targets.json](../../../benches/targets.json) 为准：

```bash
uv run python benches/run.py --list-targets
```

为每次对照固定：

- zace commit 与工作树是否有未提交修改。
- 靶场 URL、commit、golden 用例版本与样本量。
- embedding 模式、模型、维度、批大小、并发和消费者数。
- 检索预算、参数、查询向量缓存与 replay 状态。
- CPU、内存、操作系统、网络路径和数据目录所在磁盘。

质量清单的 LangChain 版本为 `41d3572`；性能复测采用 `e75dae1f53c99c2b5ddb0c7bb36022c6aea25569`。两者的文件数、chunk 数和耗时不能混用。清单中历史 projectId 只表示旧索引的绑定，不代表新机器已经具备对应索引。

外部靶场放在 zace 仓库外，只读使用其源码，遵守其自身协议。

## 3. 质量回归：首次运行

先按 [本地部署](../deployment/local.md) 安装 Python 环境，配置 embedding 凭据并加载 `.env`。准备固定版本的公共靶场，选择与它匹配的 golden 目录：

```bash
# 将路径改成已准备好的靶场和对应公共用例。
ZACE_BENCH_REPO=/绝对路径/公共靶场
ZACE_BENCH_GOLDEN=benches/golden/对应目录
ZACE_QUALITY_DATA=.local/bench/quality

uv run zace-core ingest --repo "$ZACE_BENCH_REPO" --data "$ZACE_QUALITY_DATA"
uv run python benches/run.py \
  --repo "$ZACE_BENCH_REPO" \
  --golden "$ZACE_BENCH_GOLDEN" \
  --data "$ZACE_QUALITY_DATA" \
  --report "$ZACE_QUALITY_DATA/report.md" \
  --max-tokens 10000
```

确认已有索引的模型、维度、projectId 与清单一致时，可以使用具名入口：

```bash
uv run python benches/run.py --target langchain-v1 \
  --data /已核对的索引根 \
  --report .local/bench/langchain-quality.md \
  --max-tokens 10000
```

`--target` 会展开清单中的参数；不要为了让命令跑通，随意给另一个索引套用历史 projectId。跨机复用、查询向量缓存和 `--replay` 的要求见 [benches/README.md](../../../benches/README.md)。

## 4. 质量指标与回归护栏

| 指标 | 解释 |
|---|---|
| recall@5 / recall@10 | 在前 k 个候选中命中期望依据的用例比例，以 core eval 的判定为准 |
| MRR | 首个正确命中排名倒数的平均值 |
| 负例 | 对仓库无法回答的问题，是否正确暴露证据不足 |
| 证据覆盖 | 多个必要文件或符号是否齐全；全部依据覆盖与基础 recall 分开统计 |
| 问答正确性 | 需要结合参考答案复核，不能用路径命中率代替 |

历史公共质量基线使用 Voyage 1024 维、`maxTokens=10000`：

| 靶场 | R@5 | R@10 | MRR | 负例 |
|---|---:|---:|---:|---:|
| leveldb | 1.000 | 1.000 | 0.721 | 1/1 |
| HelloAgents | 0.842 | 0.842 | 0.754 | 1/1 |
| LangChain | 0.895 | 0.895 | 0.784 | 1/1 |
| 三仓合计 | 0.912 | 0.912 | 0.756 | 3/3 |

这些是 [历史报告](../../../benches/results/README.md) 中对应版本和配置的记录，**不是当前 checkout 已重新测得的结果**。发现下降时先核对输入、指纹和降级状态，再调查实现。既有 R29/R30 参数冻结约定仍按设计与契约流程处理。

已有候选池的排序比较可离线重算，避免为小幅调参反复调用 API；具体采集与重算参数以相应工具的 `--help` 和报告说明为准。

## 5. 性能评测

准备性能手册指定版本的 LangChain 后，先做本地基线：

```bash
bash scripts/benchmark-vps.sh local /绝对路径/langchain
```

每次创建新索引，至少重复三次并记录中位数，核对输入规模、处理数量与 errors。该版本参考输入为 2,986 文件、20,931 chunks。

需要测量硬件与直连网络时，单独运行：

```bash
bash scripts/benchmark-vps.sh hardware
```

确认凭据与配额后，再执行真实 API 测量：

```bash
bash scripts/benchmark-vps.sh ttfb /绝对路径/langchain
bash scripts/benchmark-vps.sh full /绝对路径/langchain
```

结果默认写入 `.local/bench/`。硬件要求、内存护栏、阶段指标与成本说明集中在 [性能复测手册](../perf/README.md)。测量时避免同时跑测试、构建或另一轮索引。

不要用 `full - local` 直接当作网络耗时，两个流程的阶段可能重叠；不要把 API 吞吐当作 VPS 标称带宽。出现 429、OOM、降级或错误时保留失败记录，不能只报告成功样本。

## 6. LLM 问答评估约定

对于需要跨文件解释的固定问题，保存检索证据、最终回答和引用，并按以下顺序复核：

1. **事实正确**：回答中的结论能否由指定版本代码或参考答案支持。
2. **证据完整**：涉及的关键调用方、被调用方或规则是否覆盖。
3. **引用有效**：路径、行号或证据 ID 是否对应实际给出的内容。
4. **不足诚实**：负例或缺少依据时，是否明确说明无法确认。
5. **成本与延迟**：记录模型、协议、token 用量和耗时；未提供的指标写“未测”。

固定问题、模型、上下文预算和采样配置后再做对照。`expected_mode=all` 等 QA 指标与 core recall/MRR 的定义不同，报告时分别呈现。引用有效也不自动意味着语义正确。

## 7. 报告与提交

每份报告至少包含：

```text
评测目标与 UTC 日期：
zace commit / 工作树状态：
靶场 URL / commit / 文件与 chunks 数：
golden 版本 / 样本量：
设备 / 操作系统 / 数据盘：
embedding / LLM / 检索预算 / 并发配置：
冷启动或暖启动 / 缓存与 replay 状态：
执行命令与运行次数：
质量指标 / 墙钟 / CPU / RSS / API 用量：
错误、降级、未测项与结论：
原始结果位置：
```

原始数据保留在 `.local/`，经审查的公开摘要放在 `benches/results/` 并登记索引。内部用例、答案和索引通过仓库外私有清单显式加载，不进入公共默认流程。

本篇是方案和操作入口；仅整理文档不代表重新执行了评测，也不产生新的性能结论。
