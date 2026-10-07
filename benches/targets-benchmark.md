# 靶场登记：benchmark/{leveldb,HelloAgents,langchain}

> 状态：**当前基线靶场**（2026-09-15 起）｜ 登记人：实施 AI（TASK-102）
> 本文是 `benches/README.md`「当前靶场」一节的延伸：旧靶场（2026-09-14 清理）见该文「靶场变更」。

## 为什么是这三个

用户 2026-09-15 指示："以后就基于这三个仓库进行测试，本机是公司电脑环境不会再变化。"

选型覆盖**规模梯度 + 语言差异 + 真实大仓库**三个维度：

| 档 | 仓库 | 主语言 | 规模 | 选取理由 |
|---|---|---|---|---|
| 小 | `leveldb` | C/C++ | 152 文件 / 1.9K chunks | 经典 C++ 仓库；**平均 chunk 最短**（137 tok/chunk），与 Python 仓库形成对照 |
| 中 | `HelloAgents` | Python + Markdown | 236 文件 / 2.7K chunks | 教程仓：文档与代码一一对应，覆盖 spec 检索与中英对照（与 `benches/golden/hello-agents` 同源） |
| 大 | `langchain` | Python | 2,950 文件 / 20.7K chunks | **真实 monorepo 规模**；同时含大量二进制/超限文件（173 个被跳过），是索引范围策略的活标本 |

## 路径与 commit（设备绑定）

三个靶场位于**仓库外部** `/home/xuwenzheng/2_github/AI/ACE/benchmark/`，**只读**：
不在其中建文件、不修改、不把源码纳入 nova 仓库。

| 仓库 | commit（2026-09-15 记录） | 远端 | 目录体积 | 其中 `.git` |
|---|---|---|---|---|
| `HelloAgents` | `93e77ea` | `github.com/jjyaoao/HelloAgents` | 5.6 MB | 2.0 MB |
| `leveldb` | `7ee830d` | `github.com/google/leveldb` | 3.5 MB | 2.2 MB |
| `langchain` | `41d3572` | `github.com/langchain-ai/langchain` | 644 MB | **593 MB** |

> ⚠️ langchain 的 644 MB 里 **593 MB 是 `.git`**，可索引范围只有 4.73M token。
> 引用"仓库大小"时必须区分：**chunk 数才是耗时指标，不是目录体积。**

## 画像（本地统计，免 API）

| 档 | 仓库 | 文件 listed/parsed | chunks | tokens | tok/chunk | token p50/p90/p99/max |
|---|---|---|---|---|---|---|
| 小 | `leveldb` | 152 / 152 | 1,898 | 0.26M | 137 | 39/271/2527/4967 |
| 中 | `HelloAgents` | 240 / 236 | 2,729 | 0.89M | 328 | 168/655/2920/3498 |
| 大 | `langchain` | 3,123 / 2,950 | 20,673 | 4.73M | 229 | 129/474/1881/7005 |

复现：`uv run python benches/embed-bench/profile_repo.py --repo <路径> --out out.json`

> 画像脚本已对齐 indexer 口径：**C++ 仓库的 `.h` 按 `cpp` 解析**（R1 仓库级语言抬升）。
> 未对齐时 leveldb 少算 513 chunks（1898 vs 1385）——这是最初的口径误差，已修正。

## 耗时基准

- 公司 WSL：`benches/results/index-cost-model-company-wsl.md`（设备绑定 `company-wsl`，硅基流动 bge-m3）；
- **VPS 量产环境**：`benches/results/index-cost-model-vps.md`（设备绑定 `vps-la-2c2g`，Voyage voyage-4-lite）。

## 持久索引（VPS，2026-09-15 起）

> 设备：`vps-la-2c2g`（2 vCPU / 1.9 GiB / 内核 5.15.0-191 / 洛杉矶）｜embedding：`api:voyage-4-lite@1024`
> 五项留档指标（Total Tokens / TPM / Chunks / Response MB / API→VPS MB/s）与内存约束见
> `benches/results/index-cost-model-vps.md`；机器可读清单：`/root/.nova/bench/voyage-4-lite-d1024/INDEXES.json`。

**三个索引已建好并落在持久目录，未来测试一律复用（不再 ingest）**：

| 档 | 仓库 | commit | projectId | chunks | 索引体积 | 冷启动墙钟 | 峰值 RSS |
|---|---|---|---|---|---|---|---|
| 小 | `leveldb` | `7ee830d` | `3ed886ce58bc0e47` | 1,898 | 13 MB | 8.9s | 504.6 MB |
| 中 | `HelloAgents` | `93e77ea` | `06078cc80c7ce7d7` | 2,729 | 28 MB | 16.9s | 554.9 MB |
| 大 | `langchain` | `41d3572` | `ca2050db0db5b1e2` | 20,673 | 179 MB | 115.9s | 928.0 MB |

数据根按**模型 + 维度**分目录（`<model>-d<dim>`）：换模型或换维度即换数据根，互不污染、
也不会因指纹不符触发 D-07 重嵌。当前根：`/root/.nova/bench/voyage-4-lite-d1024`。

```bash
DATA=/root/.nova/bench/voyage-4-lite-d1024
uv run nova-core search "<query>" --project-id ca2050db0db5b1e2 \
  --repo /root/xuwenzheng/ACE/benchmark/langchain --data "$DATA"
```

- 缺索引时用 `bash benches/embed-bench/build_indexes.sh`（**已建的自动跳过、不重嵌**，
  脚本即"不再索引"的执行者）；`--dry-run` 只看不动；
- **这台机器 2 GiB，并发用 `EMBED_CONCURRENCY=4`**（registry 默认 8 会让窗口翻倍到 4,000
  chunk、峰值 ~1.19 GB 并触发 cgroup 回收，langchain 7 分钟都跑不完）；吞吐几乎无损
  （conc=4 → 4.46 MB/s，conc=8 → 4.48 MB/s）；
- 三个索引合计：25,300 chunk、4.58 M API token（bge-m3 口径 5.88 M）、响应体 307.8 MB。
- **耗时归因**：冷启动 = 本地 2/3 + 网络 1/3（langchain：84.6s 本地 + 37.1s 网络 = 115.9s），
  瓶颈是 2 vCPU 而非带宽；TPM 用不满是结构性上限（跑满 16 M 需 18.5 MB/s，本机链路只有 9.2–11.6 MB/s）。
  实测见 `benches/results/index-cost-model-vps.md` §3。

## 质量题库与统一入口（2026-09-15 补齐）

- 三仓库各 20 题，共 60 题；当前 v2 题目、答案与依据见
  `benches/golden/{leveldb,HelloAgents,langchain}/qa.md`；
- 审计、v2 基线和 core 建议见 `benches/results/qa-audit-2026-09-15.md`；
- 三个持久索引已登记进 `targets.json`，名字分别是 `leveldb-v1`、`helloagents-v1`、
  `langchain-v1`。可用 `uv run python benches/run.py --list-targets` 查看，直接 eval，不重建索引。
