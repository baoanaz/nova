# bench — 真实仓库的端到端基准

用六个公开仓库（C / C++ / Python / TypeScript / Rust / Kotlin 各一）共 60 道题，
评估 **整条检索链路在真实代码上的表现**：解析 → 切块 → 嵌入 → 召回 → 融合 → rerank → 装填 → LLM 总结。
靶场清单、固定 commit 与出题原则见 [靶场清单](../docs/handbook/benchmark/targets.md)。

它与 [`benches/`](../benches/README.md)（golden 集上的 recall@k / MRR 回归、性能复测）互补，不是替代：

| | `benches/`（golden 回归） | `bench/`（真实仓库） |
|---|---|---|
| 回答的问题 | 检索排序参数改动后是否回归 | 在没见过的真实仓库上能不能找到、答得对不对 |
| 题量与来源 | 少量人工标注，固定语料 | 每仓 5 道 search + 5 道 ask，答案都有行号依据 |
| 指标 | recall@5/10、MRR、负例 | 命中名次、事实覆盖、引用覆盖、延迟 |
| 适合 | CI 回归、调参对照 | 发现解析/切块/索引范围这类“根本没进索引”的系统性问题 |

## 运行

需要 `~/.key/nova/secrets.env`（`EMBED_*`；ask 另需 `ANSWER_*`）和克隆好的靶场
（`~/github/ace/bench-repos/`，commit 固定，见靶场清单）。以下命令都在仓库根目录执行。

```bash
bash bench/stack.sh up                        # 无界面本地服务（nova-service local，端口 8892）
.venv/bin/python bench/run.py list            # 靶场与题目
.venv/bin/python bench/run.py index           # 接入并建索引（幂等；重复运行只做增量）
.venv/bin/python bench/run.py run --label x   # 全部 60 题；可加 --repo/--tool/--ids
.venv/bin/python bench/run.py compare --base 旧目录名 --new 新目录名
.venv/bin/python bench/run.py diagnose --repo fastapi   # 期望依据在候选池与各通道的名次
bash bench/stack.sh down
```

结果写入 `bench/results/<时间戳>/{results.jsonl,report.md,meta.json}`（已被 `.gitignore` 忽略）。
`stack.sh down` 之后再 `up` 请间隔几秒，等旧进程退出。

## LLM 审核（出新题、不看固定题库）

```bash
.venv/bin/python bench/run.py judge --n 4 --seed 1          # 每个仓库随机抽 4 个函数出题
.venv/bin/python bench/run.py judge --repo fastapi --tools search
```

流程：从索引里随机抽函数（排除测试/示例/文档）→ LLM 写一个“不含标识符”的问题并给出应有事实 →
调用真实 `search` / `ask` → LLM 按评分表打分（search：是否包含依据、相关度、够不够答、噪声；
ask：正确、有依据、有用、是否幻觉）并指出最主要的问题和一个改进建议。
输出 `bench/results/judge-<时间戳>/{cases.jsonl,report.md}`，`cases.jsonl` 保留工具原始输出。
LLM 打分只当发现问题的线索，需要人工抽查原文；换 `--seed` 可以得到另一批题。
LLM 用 `secrets.env` 里的 `ANSWER_*`（与 ask 同一个模型）。

## 评分口径（自动粗筛，不是最终裁判）

- **search**：期望依据（文件，可带符号）在返回证据列表里的名次；报告 命中@5 / @10、MRR。
  符号既认符号名也认 chunk 正文，所以没有专用解析器的语言也不会被误判。
- **ask**：`status == answered`、事实覆盖 ≥ 2/3、引用覆盖 ≥ 1/2。事实用关键词集合粗判；
  `results.jsonl` 保留完整答案，需要人工或 LLM 复核。
- 题目里的 `reference` 是依据代码写出的参考答案，`verified` 记录了行号证据。

## 用它排查问题的顺序

1. `diagnose` 看期望依据**是否在候选池**：不在 → 召回/索引问题；在但名次低 → rerank 问题。
2. 候选池里连文件都没有 → 先查 `files` 表该文件有没有被索引（大小上限、忽略规则、解析器）。
   这个办法发现了 128 KB 上限丢掉 `routing.py` / `forwarder.rs`、Rust/Kotlin 没有解析器两个根因。
3. ask 出现 `degraded` → 看服务日志里的 `ask 降级` 原因。

## 环境变量

| 变量 | 作用 |
|---|---|
| `NOVA_BENCH_DATA` | 索引数据根，默认 `~/.nova/bench-data` |
| `NOVA_BENCH_PORT` | 服务端口，默认 8892 |
| `NOVA_BENCH_REPOS` | 靶场克隆目录，默认 `~/github/ace/bench-repos` |
| `NOVA_SECRETS_FILE` | 密钥文件，默认 `~/.key/nova/secrets.env` |

回环流量只在 `stack.sh` 启动的子进程里绕过宿主机代理，不修改任何全局网络设置。
