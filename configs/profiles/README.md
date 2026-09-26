# 硬件配置档案（`configs/profiles/`）

> **这一层解决什么**：`EMBED_BATCH_SIZE` / `EMBED_CONCURRENCY` / `ZACE_EMBED_WORKERS` 这几个旋钮
> **随硬件变化**（核数、内存、网速），并且直接是"索引内存峰值"与"冷启动时间"的乘数。
> 以前它们散落在各机器的 `/etc/zace/zace.env` 与文档正文里，换机时既无法对照也无法回滚。
> 这里把**非密钥**的调优取值按机器命名、进 Git 留档；密钥仍只留在 `/etc/zace/zace.env`（0600，永不进 Git）。

## 用法

```bash
# 本地 / 基准
set -a; source /etc/zace/zace.env                     # 密钥 + 服务形态（不进 Git）
set -a; source configs/profiles/<档案名>.env          # 本机调优档（进 Git）
set +a
```

后 source 的覆盖前者（同名键）。systemd 部署：在 `EnvironmentFile=/etc/zace/zace.env` **之后**
再加一行（`-` 表示文件缺失不报错）：

```ini
EnvironmentFile=-/opt/zace/app/configs/profiles/<档案名>.env
```

## 命名约定

**档案名 = 机器标识**：优先用 IP（如 `154.12.34.214`）；还没拿到 IP 的用规格占位
（如 `dmit-2c2g-200m`），上机后按 IP 复制一份新档、把占位档留在表里当历史（README §档案清单）。

## 旋钮是什么

| 键 | 缺省 | 作用 | 对内存/时间的影响 |
|---|---|---|---|
| `EMBED_BATCH_SIZE` | 64（Voyage 档 500） | 单次 HTTP 请求最多几条文本 | 与并发一起决定"窗口"大小 |
| `EMBED_CONCURRENCY` | 厂商档（Voyage 8） | **单次 `embed()` 调用内**的并发批数 | 在飞请求数；内存 × |
| `ZACE_EMBED_WORKERS` | 2（夹在 1..4） | 向量阶段**消费者线程数**（TASK-115） | 同时在处理的窗口数；网络与解码/落库的重叠度 |
| `EMBED_BATCH_TOKEN_BUDGET` | 8192（Voyage 档 300000） | 单请求 token 预算（与 batch_size 取先到者） | 影响请求数与尖峰 |
| `EMBED_MAX_INPUT_TOKENS` | 模型上限 | 单条输入截断上限 | 不影响内存峰值 |

**窗口 = `EMBED_BATCH_SIZE × EMBED_CONCURRENCY`**（chunk 数，`MAX_EMBED_WINDOW=4000` 封顶）。
TASK-115 起窗口由**主线程攒满整窗再派发**给 `ZACE_EMBED_WORKERS` 个消费者：

- 向量在飞 ≈ `ZACE_EMBED_WORKERS × 窗口 × 33KB`（1024 维 float 的 Python 形态，33KB/条）；
- ⚠️ **不要把窗口按消费者切小**：provider 的并发是"单次 `embed()` 调用内"的，窗口切半会让每次调用
  打不满并发，实测零收益（`benches/results/index-perf-task115-vps.md` §2 有负结果）。

## 内存峰值怎么估

实测（同一台 2 vCPU / 1.9GiB，langchain 20931 chunk，真实 API）：

| 配置 | 峰值 RSS | 冷启动 |
|---|---|---|
| 窗口 2000 × 1 消费者 | 748MB | 79.5s |
| 窗口 2000 × 2 消费者 | 823MB | 63.3s |

**不要拿线性公式外推**（进程基础、解析器、Arrow/LanceDB 缓冲、SQLite WAL 各占一块）。
做法：在目标机上用内存护栏 + 探针实测：

```bash
systemd-run --scope --quiet -p MemoryHigh=1400M -p MemoryMax=1700M -p MemorySwapMax=1024M -- \
  uv run python benches/embed-bench/coldstart_probe.py --repo <仓库> --data "$(mktemp -d)" \
  --out /tmp/x.json --mode local-only     # 免费；去掉 --mode 走真实 API
```

## 档案清单

| 档案 | 机器 | 状态 | 关键取值 |
|---|---|---|---|
| [`154.12.34.214.env`](154.12.34.214.env) | 现役 VPS：2 vCPU Xeon E5-2680v4 / 1.9GiB / 出口实测 ~90Mbps | ✅ 已实测冻结（2026-09-26 复核） | 窗口 2000、K=2、MemoryMax=1400M |
| [`dmit-2c2g-200m.env`](dmit-2c2g-200m.env) | 计划中的 DMIT：2 vCPU / 2GiB / 200Mbps | ⏳ 待上机实测 | 窗口 2000、K=2（内存允许可试 3）、MemoryMax=1500M |
