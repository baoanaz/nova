# benches/embed-bench — 索引耗时/吞吐的计量工具

> 用途：把"索引为什么慢、慢在哪、能优化多少"变成**可复现的数字**。
> 结论与留档见 `../results/index-cost-model-vps.md`（VPS）与 `../results/index-cost-model-company-wsl.md`（WSL）。
> 脚本只读靶场；coldstart/ingest/local-only/build_indexes 会写指定的数据根。
> 新 VPS 入口见 `scripts/benchmark-vps.sh`，本机产物统一落 `.local/bench/`。

## 默认离线回放

日常开发、CPU/存储与架构测试默认使用 `sync_probe.py`，不加载 API 凭据。只有明确测试远端 embedding/API 性能时，才使用下面标为联网的探针。

`replay.py` 从**已有真实索引**导出本地 SQLite 向量文件，按完整 embedding 输入的 SHA-256 查找 float32 向量。缺失输入、损坏维度直接报错，**没有网络回退**。文件记录模型、维度、输入上限和语料版本，放在忽略目录 `.local/fixtures/`，不提交到 Git。

```bash
.venv/bin/python benches/embed-bench/replay.py \
  --project .local/bench/tpm-16m/full-c4/index/projects/4c0617fc2fac1add \
  --out .local/fixtures/langchain-voyage-4-lite.sqlite \
  --corpus-commit e75dae1f53c99c2b5ddb0c7bb36022c6aea25569
```

以上本机 fixture 已导出，可直接复用，不必重复导出。更改语料或模型时需要匹配的新 fixture，不能自动调用 API 补齐。

回放保留解析、SQLite/FTS、真实向量物化、LanceDB 和图解析；**不包含远端推理、网络下载、HTTP JSON 解码**，不能直接当成线上全程时间。`--kind client` 通过真实 Rust MCP 客户端连接隔离的本机服务，包含上传、后台等待及查询；其查询复用 passage 向量只用于走通链路，**不用于检索质量评估**。

## 脚本清单

| 脚本 | 回答什么问题 | 联网 | 写索引 | 成本 |
|---|---|---|---|---|
| `replay.py` | 从已有索引导出真实向量 fixture；不调用 provider | ❌ | 只写 fixture | 免费 |
| `sync_probe.py` | `core` 整仓索引或 `client` 真实用户路径的离线回放 | 仅 client 的本机回环 | ✅（新目录） | 免费 |
| `build_indexes.sh` | 三靶场的**持久索引**建/复用（未来测试的唯一入口，缺哪个建哪个） | ✅ | ✅ | 烧 token（首次） |
| `ingest_probe.py` | 真实 ingest 路径 + 进程内计量：响应 MB / API token / 网络在飞 / 嵌入窗口 / upsert / 峰值 RSS | ✅ | ✅ | 烧 token |
| `local_only_probe.py` | **零网络地板**：解析+切分+SQLite/FTS+建图+入库要多久（判定"瓶颈是不是网速"） | ❌ | ✅（临时根） | **免费** |
| `coldstart_probe.py` | **阶段级分解**：本地段/网络段逐阶段墙钟（jieba、SQLite、JSON 解码、LanceDB、背压等待）、RSS 曲线、HTTP TTFB/下载细分 | full 联网，local-only 离线 | ✅（临时根） | local-only 免费 / full 烧 token |
| `hardware_probe.py` | sysbench 单/双线程与直连 OVH 下载、Cloudflare 上传（不发送源码） | 仅测速 | ❌ | 约 540 MB 流量，无 API 配额 |
| `ttfb_probe.py` | 只调 `/v1/embeddings`、不落盘：并发下的 TTFB 与聚合吞吐分解 | ✅ | ❌ | 少量 token |
| `throughput_probe.py` | 固定样本的批量吞吐扫描（并发 / 批大小 / 预算） | ✅ | ❌ | 少量 token |
| `profile_repo.py` | 仓库画像：文件 / chunk / token 分布（免 API，出题与估算用） | ❌ | ❌ | 免费 |
| `run_targets.sh` | WSL 侧三靶场一键跑批（历史工具，产物在 `~/.cache/nova-bench`） | ✅ | ✅ | 烧 token |

## 指标口径（**引用数字前必须先对齐口径**）

| 字段 | 定义 | 常见误用 |
|---|---|---|
| `ingest.wall_s` | `engine.ingest_repo()` 的墙钟（探针跑法） | 与 CLI `nova-core ingest` 的墙钟**不等价**：探针额外做全量 tokenize，`vps-la-2c2g` 上高 ~19% |
| `network_busy_s` | **所有** HTTP 请求在飞区间的**并集** | 并发下不能把每请求耗时相加（会重复计时） |
| `embedding_window_s` | `provider.embed()` 调用窗口（含窗口内非网络部分） | 它 ≥ `network_busy_s`，两者之差是窗口内的本地开销 |
| `api_mb_per_s_network_busy` | `response_mb ÷ network_busy_s` | **推荐口径**；历史上用过的"÷ 嵌入窗口"口径已作废 |
| `response_mb` | `CountingClient` 累计的响应体字节（MiB） | 不含请求体；请求体只有响应体 ~1/17 |
| `api_total_tokens` | API 返回的 `usage.total_tokens` 累加 | 与 `tokens_sent_tokenizer`（bge-m3 口径）不是一回事 |
| `peak_rss_mb` | `resource.getrusage().ru_maxrss`（**进程**峰值） | 不是整机内存；与 CLI `/usr/bin/time -v` 口径有差异 |
| `upsert_total_s` | `VectorStore.upsert` 打桩累计 | 与解析/建图重叠，不能直接从墙钟里减 |

**TPM 折算**：`1 MB 响应体 ≈ 14,446 token`（实测 `12.46 KB/chunk` 与 `175.8 token/chunk`）。
按历史响应大小和 16M TPM 平均速率换算，持续下行约 18.5 MiB/s。
这是旧设备、旧响应格式下的预算估计；账号突发额度、服务端处理与路径吞吐都会改变结果，不能作为新 VPS 的硬性耗时下限。

## 运行纪律（2 GiB 小机器必读）

```bash
systemd-run --unit=nova-offline-core --collect --uid=root --working-directory="$PWD" \
  -p MemoryHigh=1200M -p MemoryMax=1500M -p MemorySwapMax=256M \
  -p IPAddressDeny=any -p IPAddressAllow=localhost -- \
  .venv/bin/python benches/embed-bench/sync_probe.py \
    --kind core --repo ../benchmark/langchain \
    --fixture .local/fixtures/langchain-voyage-4-lite.sqlite \
    --out .local/bench/offline-core-new
# --kind client 测用户路径；输出目录必须不存在。日志通过 journalctl -u nova-offline-core 查看。
```

- `MemoryMax` 是保命线：2026-09-15 04:06 有一次全量向量 ingest 把机器拖到失联（见 VPS 报告 §4.1）；
- `MemoryHigh` 别低于 1200M（设 700M 会让 langchain 7 分钟跑不完）；
- 建索引**串行**（单 key 独占），别并行跑两个仓库污染计量；
- `pytest` 要用 `uv run python -m pytest -o addopts="" -q`（缺 console script，且根 `addopts=-q` 会吞汇总行）。
