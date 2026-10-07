# 基准测试

本目录包括检索质量回归与索引性能计量。外部靶场只读，源码和索引不提交到 nova。
安装见 [ENVIRONMENT.txt](../ENVIRONMENT.txt)，报告索引见 [results/README.md](results/README.md)。

## 入口

| 目标 | 入口 |
|---|---|
| 新 VPS：本地索引、CPU 与网络 | [性能手册](../docs/handbook/perf/README.md)、`scripts/benchmark-vps.sh` |
| 检索质量回归 | `run.py`、`targets.json`、[质量手册](../docs/handbook/benchmark/README.md) |
| 原始指标定义 | [embed-bench/README.md](embed-bench/README.md) |
| 冻结的质量配置与旧设备基线 | [results/baseline-v1.md](results/baseline-v1.md) |
| 模型选型工具 | `bakeoff/` |

当前默认清单只包含公共靶场和本仓自检。内部问答、用例、报告与清单已迁至
`$HOME/.key/nova/benchmarks/`，需要时显式指定外部清单：

```bash
uv run python benches/run.py --list-targets
uv run python benches/run.py --targets-file "$HOME/.key/nova/benchmarks/targets.json" --list-targets
```

不自动加载私有清单，不在公开仓库中保存内部源码、问题、参考答案或路径结构。
这次迁移移出了当前工作树中的已识别资料，不代表 Git 历史和其他历史证据已完成脱敏审计。

## 公开清单

| 靶场 | 角色 | 输入版本 |
|---|---|---|
| `hello-agents` | primary，教程仓 | `4f7682ceafe5` |
| `leveldb-v1` | primary | 见 `targets.json` |
| `helloagents-v1` | primary | 见 `targets.json` |
| `langchain-v1` | primary，质量回归 | `41d3572` |
| `nova` | dogfood | 当前工作树 |

性能复测使用 LangChain `e75dae1f`（2986 文件 / 20931 chunks），
不能与质量清单 `41d3572`（2950 文件 / 20673 chunks）的数字混用。

`targets.json` 中旧 projectId 是对应历史索引的绑定，不意味着新机器已经有索引。
只有确认数据根和指纹匹配时才复用；不在缺失时静默改模型或重新嵌入。

## 首次建立公共索引

配置真实 embedding 凭据，固定靶场版本后：

```bash
set -a; source .env; set +a
uv run nova-core ingest --repo <公共靶场路径> --data .local/bench/quality
uv run python benches/run.py --golden benches/golden/<公开用例目录> \
  --repo <公共靶场路径> --data .local/bench/quality --report .local/bench/quality/report.md
```

2 GiB VPS 上索引操作需要内存护栏，详见性能手册。
确认已有索引时，可以使用绑定的清单名称：

```bash
uv run python benches/run.py --target langchain-v1 \
  --data <已经核对的索引根> --report .local/bench/quality/report.md
```

`--project-id` 用于 benchmark 复用，会跳过仓库身份推导；不能据此把别的分支或维度的索引视为正确。
本机新绑定可以记在外部清单，再通过 `--targets-file` 加载。
靶场来源、commit、模型/维度、配置和设备必须写进报告。

## 用例与指标

`golden/<repo>/*.jsonl` 保存公共查询和期望路径/符号，`qa.md` 保存公共参考答案。
一条用例的主要字段为 `id`、`repo_hint`、`commit`、`query`、`lang`、
`category`、`expected` 和 `notes`。`expected` 绑定靶场和版本，不能跨仓复用。

- recall@k：命中用例比例；MRR：首个命中排名倒数的平均值。
- 负例单列，不能用“返回了名字相近的文件”冒充可回答。
- `expected_mode=all` 由 QA probe 统计全部依据覆盖，不改变 core 的 recall/MRR。
- `answerable=false` 时 ask 不调用 LLM，记录 insufficient_evidence。
- 路径字符串命中不等同于答案正确率；有效引用也仍需要语义核对。

命中判定集中在 core eval，runner 只做清单展开与参数透传。

## 查询向量与跨机复用

`--vector-cache` 将查询向量放在本机忽略目录，可减少重复请求；
`--replay` 仅回放预热的查询，未命中时必须明确降级，不能换通道冒充同口径。
索引、侧车和配置的模型/维度指纹必须一致。

```bash
uv run nova-core eval --repo <靶场路径> --data <索引根> --project-id <id> \
  --golden <用例目录> --report .local/bench/preheat.md \
  --vector-cache .local/bench/query-vectors.json
```

索引中的 `chunks.content` 是源码正文，bundle 相当于源码副本。
仅在有明确共享权限的环境使用 `scripts/bench-bundle.sh`；
内部用例、报告、向量侧车和索引都保持私有，不能因为“不含原始文件”就公开。
生产系统和公开机器不接收内部靶场副本。

## 验证与维护

```bash
uv run pytest -o addopts="" -q benches/test_targets.py benches/test_run.py
```

测试不联网、不建真实索引。新增公共靶场时同时登记用例和版本；
新增内部靶场时只修改外部私有清单。
报告中的每个数字绑定设备、日期、代码版本、输入规模与配置，历史数字只用于对应版本。
