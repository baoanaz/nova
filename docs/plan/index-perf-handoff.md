# 索引冷启动性能 Handoff（2026-09-25 · 调研完成，待优化）

> **给明天接手优化的会话**：这份文档只讲"慢在哪、怎么改、怎么验收"。
> 全部数字都是**本机实测**，不是估算；原始计量 JSON 已归档在 `/root/.zace/bench/probe-2026-09-25/`（`/tmp` 的副本会在重启后丢失）。
> 读之前先读根 `HANDOFF.md` 与 `benches/README.md`；改代码前先读 `AGENTS.md`（工作区隔离 / 契约流程）。

---

## 0. 一句话结论

> **冷启动 = 本地 CPU 段（langchain 53% / HelloAgents 70%）+ 网络段（29%）+ 建边（6%），两段严格串行、零重叠。**
> 本地段里最大的一块是 **jieba 预分词（langchain 36.4s = 27.7%）**，而 langchain **99.9% 的内容不含 CJK——这部分是纯浪费**。

三个可直接下手的浪费点（合计约 **-53s / langchain**）：
1. **jieba 对纯英文代码做无用功**：36.4s
2. **整仓向量以 Python list 常驻**：+823MB 峰值，还拖慢 SQL 段
3. **冷启动时"定位旧行 + 删旧行 + 每文件独立提交"**：11.4s（行数据插入本身只要 1.1s）

---

## 1. 环境与口径（引用数字前必读）

| 项 | 值 |
|---|---|
| 设备 | `vps-la-2c2g`：**2 vCPU Intel Xeon E5-2680 v4 @2.4GHz** / 1.9 GiB / 内核 5.15.0-191 / BBR+fq / rmem+wmem 32MB |
| 链路实测 | **单流 11.21 MB/s，4 流并行聚合 11.64 MB/s**（→ 出口被硬限速在 ~90–93 Mbps，与流数无关） |
| 上行 | 5.70 MB/s |
| embedding | `api:voyage-4-lite` / 1024 维 / `batch=500` / `budget=300000` / **`EMBED_CONCURRENCY=4`** / maxTok 32000 |
| 配额 | 16,000,000 TPM / 2,000 RPM（全程 200，无 429） |
| 靶场 A | `HelloAgents @ 93e77ea`：236 文件 / 2,729 chunk |
| 靶场 B | `langchain @ e75dae1f`（2026-09-25，**比 `INDEXES.json` 里登记的 `41d3572` 新**）：2,986 文件 / **20,931 chunk** / 15.4M 字符 |
| 口径 | "服务端口径"= `engine.ingest_repo()` 墙钟（**不含**进程启动/依赖导入）；"CLI 口径"= 再加 3.5–3.9s 导入 |

**换算常数（voyage-4-lite @1024，实测）**
- `1 chunk ≈ 175.8 token`，`1 chunk = 12.46 KB 响应体` → **1 token → 70.9 字节向量**
- **跑满 16M TPM 需要 18.9 MB/s 持续下行**（= `TPM ÷ 846`）；加上并发/延迟余量 → **≥40 MB/s**
- 内存：**峰值(MB) ≈ 285 + 0.04 × chunk数 + 42 × 并发**

---

## 2. 现状基线

### 2.1 HelloAgents（2 轮，±0.3%）

| 阶段 | 秒 | 占比 |
|---|---|---|
| 进程启动 + 依赖导入（**仅 CLI 口径**） | 3.5–3.9 | — |
| 扫描 + 读文件 | 0.06 | 0.4% |
| tree-sitter 解析 + chunk 切片 | 1.25（另一轮 1.36） | 7.4% |
| **SQLite / FTS 写入** | **8.41**（另一轮 8.83） | **50.1%** |
| └ 其中 **jieba 预分词** | **7.4–7.7** | **~45%**（含首次建词典 1.33s） |
| 向量段（嵌入窗口 5.6 + LanceDB 0.47 + 解码/归一化） | 6.57 | 39.1% |
| 二阶段建边/引用 | 0.27 | 1.6% |
| **ingest 墙钟** | **16.80 / 16.85** | |
| CLI 墙钟 | 20.4–20.5 | |
| 峰值 RSS | 559–569 MB | |
| 网络 | 11 请求 / 33.2MB 下行 / 3.1MB 上行 / 738,653 token / 在飞 4.8–5.1s / 聚合 6.9 MB/s | |
| TPM | 窗口内 7.5M（配额 47%） | |

**增量对照**：无变更 → 工作 **0.29s**，但 CLI 墙钟 **4.0–4.4s**（全是固定导入开销）；改 1 个小文件 → 工作 ~0.5s（向量按内容复用 26/27），用户可见 ~5s。

### 2.2 langchain（默认配置，2026-09-25 实测）

| 阶段 | 秒 | 占 131.5s |
|---|---|---|
| 扫描+读盘 | 1.4 | 1.1% |
| tree-sitter 解析 | 6.0 | 4.6% |
| chunk 切片 | 1.4 | 1.1% |
| **SQLite/FTS 写入** | **61.0** | **46.4%** |
| └ **jieba 预分词** | **36.4** | **27.7%** |
| └ FTS 其他 | 1.5 | 1.1% |
| └ **非 FTS SQL** | **23.1** | **17.6%** |
| 向量段 | 50.2 | 38.1% |
| └ **网络在飞（并集）** | **37.7** | **28.7%** |
| └ JSON 解码 | 8.5 | 6.5% |
| └ float 转换 + L2 | 3.0 | 2.2% |
| └ LanceDB upsert | 4.4 | 3.3% |
| 二阶段建边/引用 | 8.0 | 6.1% |
| 收尾/其他 | 3.5 | 2.7% |
| **ingest 墙钟** | **131.5** | |
| CLI 墙钟 | 136.7 | |
| CPU 累计 / 峰值 RSS | 98.1s（**75% 单核**） / **1,138.6 MB** | |
| 网络 | 63 请求 / **254.7MB 下行** / 16.3MB 上行 / 3.66M token | |
| 网络三段 | 上行 2.9s → 墙钟 0.7s ｜ 服务端计算 38.4s → 墙钟 9.6s ｜ **下行 74.6s → 墙钟 18.7s** | |
| 聚合吞吐 | **6.75 MB/s**（链路上限 11.3 的 60%） | |
| TPM | 窗口 4.86M（30%）/ 在飞 5.83M（36%）/ 整段 1.67M（10%） | |

### 2.3 SQL 微回放（空库、干净环境、不含 FTS，`sql_replay.py`）

| 项 | 秒 |
|---|---|
| 每文件 3 次"定位旧行"SELECT | 4.16 |
| 每文件 DELETE（4 张表 + edge 源） | 3.96 |
| COMMIT（2,986 个独立事务） | 3.32 |
| **20,931 行 chunk 逐行 INSERT** | **0.43** |
| **52,292 行 edge INSERT** | **0.48** |
| 15,947 symbol + 960 spec_block | 0.23 |
| **合计** | **12.58** |

→ **每文件固定开销 ≈ 3.8ms × 2,986 = 11.4s；真正的行数据只要 1.1s。**
→ 实测 23.1s 与干净回放 12.6s 的差额 ≈ 10.5s，主要是 **cgroup 内存回收写放大 + 与分词抢 CPU + WAL 增长**。

### 2.4 RSS 曲线（langchain，每 50ms 采样）

```
t=  0.0s   135 MB   进程基线
t=  5.5s   268 MB   扫描+解析
t=  5.5~70s  270→297 MB   ← 解析+SQLite 段 66 秒只涨 30MB
t= 98.6s  1121 MB   ← 嵌入段 30 秒内暴涨 +824MB
t=104.0s   676 MB   ← _embed_and_upsert 返回后立刻回落
```

---

## 3. 待优化清单（按优先级 + 依赖排序）

> 依赖关系：**P0-2（内存）必须先做**，否则 P1-4（流水线）会把峰值再推高。
> **P0-1（jieba）改变检索语义**，必须先过设计评审，不能当纯性能改动做。

### 【P0-1】jieba 预分词对纯英文/代码做无用功 —— -36.4s（langchain）

- **证据**
  - langchain `chunk.content` 无 CJK 行 **20,909 / 20,931 = 100%**；signature / docstring 同样 100%（`cjk_stats.py`）
  - HelloAgents：1136/2729 行（42%）无 CJK，跳过可省 **57%**
  - jieba 吞吐实测：**0.32–0.54 MB/s**；首次建词典 **1.33s**（每次新进程都要付）
  - 线程无效：2 线程 1.08×（GIL 绑定）
- **代码位置**
  - `core/zace_core/text/segmenter.py:32` `segment()`
  - 调用点 `core/zace_core/storage/store.py:212` `_insert_fts_row()`（对 content / signature / docstring **各调一次**）
- **方案**
  1. `segment()` 内先做 CJK 判定，无 CJK 直接返回原文（快速路径可用 `str.translate`/字节扫描实现）
  2. **查询侧必须用同一函数**（D-45）：同一规则天然自洽——含 CJK 的文本双侧都分词，不含 CJK 的双侧都原文
  3. 极端优化：整仓无 CJK 时**根本不 import jieba**（连 1.33s 建词典也省掉）
  4. 顺带的次优项：signature / docstring 与 content 高度重叠，可考虑复用（收益仅 ~0.45s，优先级低）
- **预期**：langchain **-36s**（131.5 → ~95s）；HelloAgents **-4s**
- **验收**
  - 单测：无 CJK 文本返回原文；含 CJK 文本仍分词
  - **`benches/golden` 60 题 recall@5 不低于基线 0.64–0.77**（必须跑，这是语义变更）
  - 重跑 `coldstart_probe.py` 看 `sqlite.jieba_segment` 掉到 ~0.5s
- **风险 / 纪律**
  - **这是检索语义变更**，不是纯性能改动：会改变 `chunks_fts` 内容 → 必须重建索引（触发 `full_reparse`）
  - 需要先按 `docs/plan/orchestration.md` §4 走流程修订 D-45 的口径；不要静默改

### 【P0-2】整仓向量以 Python list 常驻（内存 + 时间双杀） —— -700MB

- **证据**
  - 复现：累积 20,931 条独立 float 向量 = **+823 MB（40.3 KB/chunk）**，与 RSS 曲线上的 +824MB 完全吻合
  - 同一份向量的体积：float32 二进制 **82MB** ｜ JSON（网络形态）**255MB** ｜ **Python list[float]（内存形态）654MB**
  - 内存模型：**峰值 ≈ 285 + 0.04×chunk数 + 42×并发**
- **代码位置**
  - `core/zace_core/pipeline/indexer.py:538` `_embed_and_upsert()`：`produced.extend(...)` **跨窗口累积整仓向量**
  - 消费点 `indexer.py:527` `_embed_new()`（写 TASK-111 的跨项目 embedding cache）
  - **`_rebuild_vectors()`（`indexer.py:614`）直接丢弃返回值 → 冷启动路径纯浪费**
- **方案**：把 cache 写入**下沉到窗口循环内**（`_embed_and_upsert` 接 cache 参数，逐窗口 `put`）；或至少给 `_rebuild_vectors` 路径一个 `collect=False`
- **预期**：langchain 峰值 1138 → **~453MB**；HelloAgents 570 → ~290MB；同时省掉一次 `list(vector)` 复制的时间
- **验收**
  - `coldstart_probe.py` 看 `peak_rss_mb`
  - langchain 在 `MemoryHigh=1200M` 下不再触发回收（对比 `sqlite.apply_file_change` 从 23.1s 回到 ~12.6s）
  - **TASK-111 缓存复用仍生效**：换分支跑一次增量，确认 `chunks_reused` 命中
- **风险**：cache 是纯优化，窗口失败时已写的条目可容忍（但要在任务卡记录该语义）

### 【P1-3】冷启动时"定位旧行 / 删旧行 / 每文件独立提交" —— -8~11s

- **证据**：见 §2.3。空库上这些 SELECT/DELETE 必然全空，却占了 11.4s
- **代码位置**：`core/zace_core/storage/store.py:291` `apply_file_change()`
- **方案**
  1. 项目为空（`files` 表 0 行）时，跳过 `old_rows` / `old_symbols` 两个 SELECT + 4 个 DELETE + edge 源 DELETE
  2. 用 **SAVEPOINT** 把 N 个文件包进一个大事务（保留 TASK-018 §C 的"单文件失败隔离"：失败只回滚该 SAVEPOINT）
- **预期**：langchain 冷启动 **-8~11s**；增量路径不受影响
- **验收**：空库路径与旧路径产出**相同 FileDelta**（单测）；`pytest` 存储用例全绿；probe 看 `sqlite.apply_file_change`
- **风险**：等价性要论证（空库 DELETE 是 no-op）；合并事务不得破坏单文件失败隔离

### 【P1-4】本地与网络严格串行，零重叠 —— -28%

- **证据**：langchain 本地 ~93.8s 期间网络 **0 流量**；网络 37.7s 期间 CPU 只有 ~40% 单核利用。HelloAgents 同样（11.7 + 5.1）
- **代码位置**：`indexer.py:_run()` → 先解析全部文件，再 `_rebuild_vectors()` 全部嵌入
- **方案**：按小窗口流水线（生产者=解析/切片，消费者=嵌入+入库），**网络保持 8–16 请求在飞**；窗口大小与内存预算绑定（先做完 P0-2）
- **预期**：langchain 131.5 → `max(93.8, 37.7) ≈ 94s`；做完 P0-1 后 → **~35s**
- **验收**：`coldstart_probe.py` 看 `network_busy_s / wall` 比值；CPU 利用率从 75% 提升到 >100%（证明重叠生效）
- **风险**：峰值内存上升（依赖 P0-2）；部分窗口失败的语义要定义

### 【P1-5】CLI 固定导入 3.85s（小仓库 / 增量的体感杀手）

- **证据**：`import zace_core.cli` = **3.85s**，其中 lancedb 链 **3.16s**（`lance_namespace_urllib3_client` 自动生成客户端占 2.61s）
- **代码位置**：`core/zace_core/vectors/store.py:27` 顶层 `import lancedb`，经 `pipeline/indexer.py` → `engine` 链式导入
- **方案**：惰性导入（`TYPE_CHECKING` + 运行时 getattr 或 lru_cache loader）
- **预期**：所有 CLI 口径 **-3s**；"无变更增量"从 4.0s → ~1s
- **验收**：`/usr/bin/time` 对比 `zace-core ingest`（无变更）
- **风险**：低。注意 `pytest` 里若有 monkeypatch 依赖模块级符号需同步

### 【P2-6】解析/解码下多进程（加核的前提）

- **证据**：线程扩展性 解析 **0.94×** / jieba **1.08×** / `json.loads` **0.76×**（全部 GIL 绑定）；进程扩展性 jieba **1.94×**（2 进程）、解析 1.51×
- **方案**：进程池跑"解析+切片+（可选）预分词+JSON 解码"；**主进程只做 SQLite 写与 LanceDB 入库**
- **⚠️ 实测坑**：lancedb 0.38 在 fork 时会警告 *"fork support is experimental… a small chance of deadlock remains；建议 forkserver/spawn"*；spawn 下每个 worker 要重付 ~3.5s 导入 → **worker 不要 import lancedb**（纯 CPU 活本来也不需要），即可绕开
- **预期**：本地段再 -30~50%（配合 4 核）
- **验收**：`proc_scale.py` 风格扩展性测试 + probe 的 CPU 利用率

### 【P2-7】索引身份变更导致无谓全量重嵌（**需编排者裁决**）

- **证据**：`core/zace_core/engine.py:265` 把**分支**并进 identity material
  - HelloAgents projectId：`INDEXES.json` 记录 `06078cc80c7ce7d7`（旧算法）→ 现在算出 `5143d88f059e6c0d`
  - `bash benches/embed-bench/build_indexes.sh --dry-run` 现在把 **三个靶场全部判为"待建"** → 下次 ingest 重嵌 **4.58M token**（langchain ~116s）
  - 任何"换机器 / 换目录 / 换分支 / 改 remote"都会触发同样的全量重嵌
- **方案**（择一，属 D-29/TASK-111 冻结设计，**必须先提申请**）
  1. 身份位移时"接管"旧索引目录（按 remote + repo_path 建别名映射）
  2. 至少在 `ingest` / `--dry-run` 时输出"检测到索引位移，可能触发全量重嵌"告警

### 【P3-8】硬件采购判据（**代码优化做完再买**）

- 不优化代码只换网：网络段 37.7 → ~16s，总耗时 131.5 → **110s（-17%）**——因为剩下 93.8s 是本地 CPU，与网速无关
- 代码优化到位后再买，才能兑现到 15–17s
- **判据**：`单核性能` ≥ 现网 2× ｜ **4 vCPU** ｜ **8 GB**（下限 4GB）｜ **到 Voyage 的持续下行 ≥40 MB/s** ｜ NVMe ≥50GB
- **"1Gbps"必须实测**：按 4 流并行聚合口径（本机实测 1 流 11.21 / 4 流 11.64 MB/s → 实际是 90Mbps 口）
- **流量**：一个 langchain 冷启动 = **254MB 入站**；每天 100 次 ≈ 750GB/月 → 必须问清入站是否计费、超量是限速还是停机

---

## 4. 优化目标

| 阶段 | 内容 | langchain | HelloAgents |
|---|---|---|---|
| 现状 | — | 131.5s | 16.8s |
| **阶段 1** | P0-1 + P0-2 + P1-3 + P1-5（纯代码） | **~75s** | ~11s |
| **阶段 2** | + P1-4 流水线化 | **~35s** | ~6s |
| **阶段 3** | + P2-6 多进程 + 硬件（网速/单核） | **15–17s** | ~3s |

阶段 3 的天花板是 **TPM：3.66M token ÷ 16M TPM = 13.7s**（硬地板）。到那时再换 CPU/内存/并发都无效，只有提升 TPM 档位或减少 token 总量才有用。

**注意**：降维（1024→512）只在"链路是瓶颈"时有用；一旦 TPM 成为瓶颈，**降维完全无用**（token 数不变）。

---

## 5. 复现方法

```bash
# 探针脚本（含细粒度打桩：HTTP TTFB/下载/上行、jieba、SQLite、JSON 解码、LanceDB、建边）
P=/root/.zace/bench/probe-2026-09-25
set -a; source /etc/zace/zace.env; set +a

# 冷启动（真实 API；langchain 会烧 ~3.7M token，HelloAgents ~0.74M）
uv run python $P/coldstart_probe.py \
  --repo /root/xuwenzheng/ACE/benchmark/HelloAgents \
  --data /tmp/probe-root-hello --out /tmp/hello.json --mode full --tag hello

# 内存护栏（2 GiB 机器必挂；2026-09-15 有 langchain 拖机失联事故）
systemd-run --scope --quiet -p MemoryHigh=1200M -p MemoryMax=1500M -p MemorySwapMax=512M -- \
  uv run python $P/coldstart_probe.py --repo /root/xuwenzheng/ACE/benchmark/langchain \
  --data /tmp/probe-root-lc --out /tmp/lc.json --mode full --tag lc

# 免费的量（不烧 token）：零网络地板 / SQL 微回放 / CJK 占比 / 线程与进程扩展性
uv run python $P/coldstart_probe.py ... --mode local-only
uv run python $P/sql_replay.py <index.db> /tmp/replay/index.db
uv run python $P/cjk_stats.py <index.db>
uv run python $P/parallel_scale.py; uv run python $P/proc_scale.py
uv run python $P/mem_model.py
```

**不变量检查（改完必须全绿）**
```bash
uv run ruff check .
uv run python scripts/check_dependency_direction.py
uv run pytest -o addopts="" -q
```

---

## 6. 未决问题（需要用户 / 编排者拍板）

1. **去 jieba 是否走 D-45 修订？** 这是检索语义变更（改变 `chunks_fts` 内容），不能静默做——需要设计口径确认 + 全量 golden 回归。
2. **索引身份变更怎么处理？** D-29 / TASK-111 是冻结设计，改它要走 `orchestration.md` §4。
3. **"10 分钟"的真实场景是哪个仓库、走哪条链路？**（CLI `zace-core ingest` / client→service 上传 / 本地 ONNX）——本机单位成本是 6.3ms/chunk，10 分钟对应约 10 万 chunk。
4. **是否买 DMIT 2核2G？** 结论：**可以买，但必须先做 P0-2**（否则 langchain 每次索引都踩在换页边缘）。若预算允许，倾向 2核4G。
5. **本报告是否要落成 `benches/results/` 的正式报告**（含 HelloAgents + langchain 双靶场）？

---

## 7. 纪律提醒

- 本文件与全部探针都在**主工作区**，未提交。开工前按 `AGENTS.md` §0 认领 lane worktree，**不要在 `/root/xuwenzheng/ACE/zace` 里直接改代码**。
- P0-1 / P2-7 涉及设计口径变更，**先提申请、等裁决**，不要静默偏离。
- 改完必须回填任务卡"执行记录" + `docs/tasks/README.md` 状态，并按 `orchestration.md` §3 出报告。
- 计量口径别混：`ingest 墙钟`（服务端口径）≠ `CLI 墙钟`（含 3.5–3.9s 导入）；`网络在飞并集` ≠ 各请求耗时之和（langchain 是 37.7s vs 117.8s）。
