# benches/embed-bench — 索引耗时/吞吐的计量工具

> 用途：把"索引为什么慢、慢在哪、能优化多少"变成**可复现的数字**。
> 结论与留档见 `../results/index-cost-model-vps.md`（VPS）与 `../results/index-cost-model-company-wsl.md`（WSL）。
> 所有脚本**只读**靶场、只写 `--out` 指定的 JSON；唯一会建索引的是 `build_indexes.sh` 与 `ingest_probe.py`。

## 脚本清单

| 脚本 | 回答什么问题 | 联网 | 写索引 | 成本 |
|---|---|---|---|---|
| `build_indexes.sh` | 三靶场的**持久索引**建/复用（未来测试的唯一入口，缺哪个建哪个） | ✅ | ✅ | 烧 token（首次） |
| `ingest_probe.py` | 真实 ingest 路径 + 进程内计量：响应 MB / API token / 网络在飞 / 嵌入窗口 / upsert / 峰值 RSS | ✅ | ✅ | 烧 token |
| `local_only_probe.py` | **零网络地板**：解析+切分+SQLite/FTS+建图+入库要多久（判定"瓶颈是不是网速"） | ❌ | ✅（临时根） | **免费** |
| `coldstart_probe.py` | **阶段级分解**：本地段/网络段逐阶段墙钟（jieba、SQLite、JSON 解码、LanceDB、背压等待）、RSS 曲线、HTTP TTFB/下载细分 | ❌ | ✅（临时根） | local-only 免费 / full 烧 token |
| `ttfb_probe.py` | 只调 `/v1/embeddings`、不落盘：并发下的 TTFB 与聚合吞吐分解 | ✅ | ❌ | 少量 token |
| `throughput_probe.py` | 固定样本的批量吞吐扫描（并发 / 批大小 / 预算） | ✅ | ❌ | 少量 token |
| `profile_repo.py` | 仓库画像：文件 / chunk / token 分布（免 API，出题与估算用） | ❌ | ❌ | 免费 |
| `run_targets.sh` | WSL 侧三靶场一键跑批（历史工具，产物在 `~/.cache/zace-bench`） | ✅ | ✅ | 烧 token |

## 指标口径（**引用数字前必须先对齐口径**）

| 字段 | 定义 | 常见误用 |
|---|---|---|
| `ingest.wall_s` | `engine.ingest_repo()` 的墙钟（探针跑法） | 与 CLI `zace-core ingest` 的墙钟**不等价**：探针额外做全量 tokenize，`vps-la-2c2g` 上高 ~19% |
| `network_busy_s` | **所有** HTTP 请求在飞区间的**并集** | 并发下不能把每请求耗时相加（会重复计时） |
| `embedding_window_s` | `provider.embed()` 调用窗口（含窗口内非网络部分） | 它 ≥ `network_busy_s`，两者之差是窗口内的本地开销 |
| `api_mb_per_s_network_busy` | `response_mb ÷ network_busy_s` | **推荐口径**；历史上用过的"÷ 嵌入窗口"口径已作废 |
| `response_mb` | `CountingClient` 累计的响应体字节（MiB） | 不含请求体；请求体只有响应体 ~1/17 |
| `api_total_tokens` | API 返回的 `usage.total_tokens` 累加 | 与 `tokens_sent_tokenizer`（bge-m3 口径）不是一回事 |
| `peak_rss_mb` | `resource.getrusage().ru_maxrss`（**进程**峰值） | 不是整机内存；与 CLI `/usr/bin/time -v` 口径有差异 |
| `upsert_total_s` | `VectorStore.upsert` 打桩累计 | 与解析/建图重叠，不能直接从墙钟里减 |

**TPM 折算**：`1 MB 响应体 ≈ 14,446 token`（实测 `12.46 KB/chunk` 与 `175.8 token/chunk`）。
跑满 16M TPM 需要 **18.5 MB/s 持续下行**——比这台 VPS 的链路上限还高，故配额永远用不满。

## 运行纪律（2 GiB 小机器必读）

```bash
set -a; source /etc/zace/zace.env; set +a
systemd-run --scope --quiet -p MemoryHigh=1200M -p MemoryMax=1500M -p MemorySwapMax=512M -- \
  timeout 7200 uv run python benches/embed-bench/ingest_probe.py \
    --repo /root/xuwenzheng/ACE/benchmark/langchain \
    --data /root/.zace/bench/voyage-4-lite-d1024 \
    --out benches/results/raw/ingest-vps/langchain.json
```

- `MemoryMax` 是保命线：2026-09-15 04:06 有一次全量向量 ingest 把机器拖到失联（见 VPS 报告 §4.1）；
- `MemoryHigh` 别低于 1200M（设 700M 会让 langchain 7 分钟跑不完）；
- 建索引**串行**（单 key 独占），别并行跑两个仓库污染计量；
- `pytest` 要用 `uv run python -m pytest -o addopts="" -q`（缺 console script，且根 `addopts=-q` 会吞汇总行）。
