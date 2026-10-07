# 硬件配置档案

这里仅提交无密钥的批大小、并发、消费者和内存护栏说明。
实际密钥放 `$HOME/.key/zace/secrets.env`，机器路径和运行数据放本机 `.env`、`.local/`。
配置按规格别名命名，不使用真实 IP 或主机名。

## 使用

从仓库根加载：

```bash
set -a
source .env
source configs/profiles/epyc-2c2g.env
set +a
```

后加载的同名参数覆盖前者。`scripts/benchmark-vps.sh` 显式使用基线配置，
不根据机器型号自动抬高并发，也不让本机日常配置改变测试口径。

## 参数

| 键 | 含义 | 基线 |
|---|---|---:|
| `EMBED_BATCH_SIZE` | 每个 HTTP 请求的最大条数 | 500 |
| `EMBED_CONCURRENCY` | 单次 embed 调用内的并发批数 | 4 |
| `ZACE_EMBED_WORKERS` | 并行处理整窗的消费者数 | 2 |
| `EMBED_BATCH_TOKEN_BUDGET` | 单请求 token 预算 | 300000 |
| `EMBED_MAX_INPUT_TOKENS` | 单条输入截断上限 | 32000 |
| `EMBED_DIM` | 向量维度 | 1024 |

窗口为批大小 × 并发（当前 2000 chunks，代码上限 4000）。
TASK-115 采用主线程攒满整窗再派发；不要按消费者数把窗口切小，
否则单次调用填不满原定并发。详情见 [TASK-115 报告](../../benches/results/index-perf-task115-vps.md)。

峰值内存不能仅按在飞向量线性估计：解析器、SQLite、Arrow 和 LanceDB 都有基础与缓冲开销。
2 GiB 机器的性能测量串行执行，并使用 systemd cgroup 护栏。
基线旧机 K=1/K=2 真实 API RSS 分别为 748.4/823.4 MiB；
这些数值不是新机器的内存承诺。

## 档案

| 档案 | 设备 | 状态 |
|---|---|---|
| [vps-xeon-2c2g.env](vps-xeon-2c2g.env) | Xeon E5-2680 v4，2 vCPU，1.9 GiB | 2026-09-26 历史实测，TASK-115 |
| [epyc-2c2g.env](epyc-2c2g.env) | EPYC 7282，2 vCPU，1.9 GiB | 新 VPS 本地/硬件/API 已测；配置保持可比基线 |
| [dmit-2c2g-200m.env](dmit-2c2g-200m.env) | 历史规划规格 | 外推占位，不能当实测 |

新 VPS 数据见 [设备报告](../../benches/results/index-perf-epyc-2c2g.md)。
硬件档只说明配置来源，完整测量口径与限制在报告及 [性能手册](../../docs/handbook/perf/README.md)。
