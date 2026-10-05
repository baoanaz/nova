# M2b 波次 W6 规划：环境切换后的重定向（云端 embedding 优先 + 新靶场）

> 状态：2026-09-13 编排者制定（用户拍板四条见 §1）。
> 背景：用户更换开发环境（家里 WSL2），**旧的三靶场与全部 `/tmp` 索引产物均不存在**；
> 用户已备好硅基流动 API key 并拍板**全程改用云端 embedding**（本地 ONNX 暂缓）。
> 本篇是 M2a 收口之后、TASK-037/038 开卡之前的**方向文档**，细化 `docs/plan/phase2-roadmap.md` 的 M2b。

## 1. 本波决策（用户 2026-09-13 拍板）

| # | 决策 | 影响 |
|---|---|---|
| **U1** | **云端 embedding 为当前默认路径**：硅基流动 `BAAI/bge-m3`（1024D / 8192 token / 免费）。本地 ONNX（e5-small）**暂缓部署**，不为它消耗开发时间 | 默认值仍是 `local`（D-44 的代码默认不动），但**项目工作流**（文档、脚本、service 配置、bake-off）全部按 `EMBED_MODE=api` 走；本地路径的修复卡（TASK-038）**降级为低优先级**（保留卡、不派活） |
| **U2** | **本地配置"预留接口、暂不完善"**：D-44 的双实现（本地/API）已经存在，只需保证**未来补本地配置时不用改架构** | 不开新卡；在 §5 记录为"未来任务锚点" |
| **U3** | **预算锚点**：接受 ~10 人服务量、**月 10 元内** | 选型必须落在免费或极低价档；bge-m3 免费（BAAI 系列免费为主）→ 成本模型见 §4 |
| **U4** | **新评测靶场 = `/home/xuwenzheng/github/hello-agents`**（Python Agent 教程项目，227 md + 749 py） | 旧 golden（aibox / cameraservice）**保留在仓库但标记"靶场不可得"**；新靶场重建 golden（TASK-046） |

**U1 与 D-44 的关系**：D-44 的"本地 ONNX 为默认（源码不出 VPS）"是**部署形态**的选择，U1 是**当前开发期**的选择。
两者不冲突：代码默认值保持 `local`（不触发 D-07 无谓重嵌），实际运行时用环境变量切 `api`。
**将来若要把 api 变默认**，那是一次 L3 决策（影响指纹与既有索引），届时另开卡。

## 2. 本环境（家 WSL2）实测基线

全部数字为 2026-09-13 编制本文件时在本机实测所得，命令见各节。

### 2.1 仓库状态

| 项 | 值 |
|---|---|
| HEAD | `4dfe65c`（`origin/main` 已同步，无未推送提交） |
| 工作区 | 干净；无未提交改动 |
| 基线三条 | `ruff` clean ｜ 依赖方向 通过 ｜ **668 passed, 2 skipped**（11.4s） |
| 参考源码 `../source/` | **不存在**（任务卡里的"参考锚点"只能按卡内摘录理解） |
| 机器 | 12 逻辑核 / 15 GiB 内存 / 938 GiB 空闲磁盘 |

### 2.2 已就绪的资源

| 资源 | 状态 |
|---|---|
| 本地 e5-small ONNX | `~/.cache/huggingface/.../multilingual-e5-small` **465 MB 已下载**（历史任务遗留，本次暂不用） |
| 硅基流动 key | `~/.bashrc` 的 `zace_embeding_API_KEY`（51 字符，实测有效） |
| 网络 | 需 `NO_PROXY=127.0.0.1,localhost`（本机设了 `http_proxy=172.31.16.1:7890`） |

### 2.3 云端 embedding 实测（本波的事实基础）

实测命令（key 从 `.bashrc` 提取后注入环境）：

```bash
KEY=$(sed -n 's/^export zace_embeding_API_KEY=//p' ~/.bashrc | tr -d '"' | tr -d "'")
EMBED_MODE=api EMBED_MODEL=BAAI/bge-m3 EMBED_DIM=1024 \
  EMBED_BASE_URL=https://api.siliconflow.cn EMBED_API_KEY="$KEY" \
  uv run zace-core ingest --repo <小仓库> --data /tmp/...
```

| 指标 | 实测值 | 对照（本地 e5-small） |
|---|---|---|
| 返回维度 | 1024（与官方一致） | 384 |
| 单查询嵌入 | **94 ms** | 7 ms |
| 批量吞吐（batch 64） | **126.3 text/s**（0.51s） | 28.8 text/s（2.22s） |
| 长输入（~8000 token） | **0.27s，成功** | 512 token 上限，超出即崩（TASK-038 的成因） |
| ingest 端到端（3 文件小仓库） | **0.8s**（7 chunks） | — |
| 检索端到端 | 命中正确（`vector 0.6371 + vector rank 1`） | — |

**结论**：云端路线在本机**快 4.4 倍且无内存压力**（旧环境 bge-m3 的 3GB/int8 问题在这一路线上不存在）。

### 2.4 已实测到的三处摩擦（W6 要修的）

| # | 现象 | 影响 | 归属 |
|---|---|---|---|
| **F1** | **registry 的 `bge-m3` 条目对硅基流动是坏的**：实测 `{"model":"bge-m3"}` → API 返回 `20012 Model does not exist`；而官方 model 名 `BAAI/bge-m3` 在 registry 里**未登记** → `EmbeddingConfigError`。**两种写法都跑不通** | 用户无论照官方文档填全名、还是猜裸名，**都必然失败**；唯一可行路径是同时给 `EMBED_MODEL=BAAI/bge-m3` + `EMBED_DIM=1024` 走"未登记模型"分支 | W6-lane B（**最高优先级**） |
| **F2** | 走"未登记模型"分支时 profile 的 `max_input_tokens` = `UNKNOWN_API_MAX_INPUT_TOKENS` = **2048**，与 bge-m3 实际能力（8192）不符（已登记条目里写的是 8192，但 F1 让它无法被命中） | 长 chunk 尾部被静默截断：§2.3 已证明 8000 token 可正常嵌入 | W6-lane B |
| **F3** | key 写在 `~/.bashrc` 第 **163** 行，而第 5-9 行有 `case $- in *i*) ;; *) return;; esac` 守卫 → **非交互 shell 不导出**。实测：`bash -c 'echo $zace_embeding_API_KEY'` 为空、`bash -lc`（登录 shell）也为空；**只有交互式终端有**。而子 AI 的工具调用、CI、`scripts/*.sh` 全是非交互进程 | 子 AI 按手册跑命令会得到 401 或"key 未配置"，并**误判为 key 无效**（我今天就踩过：首次探测返回 `Token is invalid`） | W6-lane B（文档）+ §5 锚点 |
| **F4（严重，编排者 2026-09-13 全量索引实测发现）** | **`api.py` 完全不截断、且按“条数”而非“token 数”分批**：`local.py` 用 tokenizer 按 `max_input_tokens` 硬截断（`local.py:231-233`），但 `api.py` **没有任何 tokenizer/截断代码**（`max_input_tokens` 只写进指纹，不参与推理）。后果有两个：**(a)** 超长 chunk（>8192 token）→ API 返回 `400 code=20015 The parameter is invalid`，**整次 ingest 失败**；**(b)** `iter_batches` 按条数切（`base.py:75-80`），一个 batch 可能携带几十万 token → 触发 **TPM 限流 429** | **实测：`hello-agents` 全量索引失败**——1482 文件 / 9971 chunks 已入库，但**向量数为 0**（`warning: 向量索引为空`），检索降级为仅 BM25。实测该仓库有 **182 个 chunk >8192 token**（最大 **64138**）；全库嵌入总 token 约 **5.59 M** | **W6-lane B（新增 §D）** |

### 2.5 任务板状态漂移（W6 要修的）

| 现象 | 事实 |
|---|---|
| `docs/tasks/README.md` 的 Phase 2 表格里 TASK-034/035/040/045 仍是 `pending` | 但四张卡的文头均已写 `review`，且代码**已全部合入 main**（`4dfe65c` 含 TASK-045 手册） |
| 任务板波次表仍写 W5b/W5c 为"当前波次" | 实际已完成 |
| "批 11：TASK-038 → TASK-037 → 云端 embedding 接入（**R44-R46**）" | **R44 在半途被用于 TASK-034 的 attach 端点**（`openapi.yaml:109`），R45/R46 **从未存在** → 悬空引用 |

## 3. W6 波次：三条泳道

> 划分依据：文件所有权互不重叠、依赖可并行。

| 泳道 | 任务卡 | 文件所有权（互斥） | 性质 |
|---|---|---|---|
| **A** | TASK-037 索引范围策略 | `core/zace_core/pipeline/{ignore,source,indexer}.py` | 代码缺陷修复（**P0**：直接影响索引正确性与成本） |
| **B** | TASK-046 云端 embedding 接入与配置对齐 | `core/zace_core/embedding/{registry,factory,api}.py`、`service/zace_service/config.py` | 新功能 + F1 **阻断** + F2/F3 + **F4 阻断**（截断与分批） |
| **C** | TASK-047 新靶场建立与 golden 重建 | `benches/golden/hello-agents/`、`benches/results/`、`scripts/`、`docs/handbook/` | 评测基础设施（**P1**：质量工作的地基） |

**为什么 A 与 B 不冲突**：A 只碰 `pipeline/`，B 只碰 `embedding/`（+ service 配置）。`indexer.py` 在 A 里，
`factory.py` 在 B 里，无交集。

**为什么 C 独立**：C 只产出 benches/scripts/docs，不碰 `core/`。

### 3.1 泳道 A：TASK-037（索引范围策略）

**为什么是本波 P0**：`hello-agents` 是活标本——实测 `DirectorySource.list_files()` 返回 **1862 个文件**，
其中 **272 个 >128KB、345 个 .png、58 个无扩展名**；`.gitignore` 明确排除 `venv/`（已侥幸被内置目录名挡住），
但 5.0 GB 的仓库里真正该索引的只有约 976 个（py+md）。

**卡内已有**：三层忽略（`.zaceignore` > `.gitignore` > 内置）、128KB / 二进制阈值、前后对照与检索回归。
**本环境适配**：TASK-037 原卡要求的前后对照靶场（hmi / systemservice / Trellis）**已不存在**；
改用 `hello-agents` + `zace` 自身作对照靶场，在报告中说明替换理由（不造假数字）。

### 3.2 泳道 B：TASK-046 云端 embedding 接入（新卡，见 §2.4）

**为什么是本波 P0**：这是用户当前唯一可用的主路径，而实测**两处阻断级缺陷**：F1（模型名两种写法都不可用）
与 F4（`api.py` 不截断 + 按条数分批 → 真实仓库全量索引**必然失败**，`hello-agents` 上已复现：9971 chunks 但 vectors=0）。

**待办三件事**：
1. **F1（最高优先级）：让硅基流动模型真正可配**。两条路线择一，**必须在报告里写清选了哪条与理由**：
   - **(a) 别名解析**：registry 保留 `bge-m3` 为 key，新增"别名 → 实际 model 名"映射（`bge-m3` → `BAAI/bge-m3`）；
   - **(b) 改用全名作 key**：`API_MODELS` 的 key 改为 `BAAI/bge-m3`。
   **建议 (a)**：不破坏既有用户的配置（`EMBED_MODEL=bge-m3` 曾经可用），且能同时容纳多家 provider 的同名模型。
   **关键约束**：别名必须在**发请求时**替换，且 `profile.model_id` 要能区分 provider（否则换 provider 不会触发 D-07 重嵌）。
2. **F2**：`max_input_tokens` 的默认来源修正为**模型实际能力**（bge-m3 = 8192），未登记模型仍回落 2048；补测试
   （全名 + 8192 → profile 用 8192；未登记模型 → 2048；用户显式给更小值 → 不被抬高）。
3. **F3 + 文档**：写清 key 的注入方式（`.env` / 显式 export / service 配置），并把**实测数字**（§2.3）落进交付文档。
   **必须包含一条"从零照做能跑通"的完整命令序列**（含 `NO_PROXY`）。
4. **F4（新增，最高优先级与 F1 并列）：API provider 的截断与分批**——见 `docs/tasks/TASK-046-云端embedding接入.md` §D。
   这是**阻断级**缺陷：不修则任何真实仓库（含长文档）都无法完成索引。
   **本仓库已提供 `.env.example` 与 `.gitignore` 的 `.env` 排除规则**（编排者预置）。

**与 TASK-038（本地钳制）的关系**：38 修"本地模型上限钳制"，46 修"API 模型上限默认值"——**同一类缺陷的两侧**。
U1 之下 38 降级（不派人），但 46 的修法应**顺带覆盖 38 的 `min` 语义**（配置值 > 原生上限时钳制 + warning），
以便未来补本地配置时直接受益。**若 46 的实现者判断需要动 `local.py`，先在执行记录申请**（那是 38 的领地）。

### 3.3 泳道 C：TASK-047 新靶场与 golden（新卡）

**背景**：旧 golden 的三个靶场都不在本机，`benches/results/*` 的历史数字**无法复现**。

**交付**：
1. `benches/golden/hello-agents/*.jsonl`：新靶场用例（**≥ 20 条**，含正例与负例；口径见 `benches/README.md`）；
2. 每条 `expected` 必须 **grep 核验过**（沿用 TASK-014 的纪律）；
3. `benches/results/phase2-helloagents-baseline.md`：新靶场基线（reproducible，标注 commit）；
4. 旧靶场文件**保留**，在 `benches/README.md` 标注"靶场不可得，暂停"；
5. `scripts/m2a-smoke.sh`：一键冒烟（起服务 → 等索引 → MCP 调用 → 断言命中），把 F3（key 注入）写进脚本。
6. 复用 `docs/handbook/getting-started/M2a-验收手册.md` 的 §4.2 最小 MCP 客户端；**不重写**手册，只加脚本。

**hello-agents 靶场特性**（实测）：227 md + 749 py（排除 venv 后 976 个有效文件）；文档与代码**一一对应**
（`docs/chapterN/` ↔ `code/chapterN/`）→ **天然适合验证 spec 检索与 code/docs 平衡**（R21 的靶场）；
`Co-creation-projects/` 含多个独立小项目（多语言混合）。

### 3.4 本波不做

| 不做 | 原因 |
|---|---|
| TASK-038（本地 embedding 钳制） | U1/U2：本地路线暂缓；其语义并入 TASK-046 的修法 |
| TASK-023（真实数据采集） | 前置是"用户自己开始用"；等 W6 之后 demo 稳定再开 |
| TASK-050（质量调优） | 依赖 TASK-023 |
| M2c（Rust client / 鉴权 / 部署） | 用户当前是本地单人使用 |
| 本地 embedding 的完善 | U2：预留接口即可，§5 记录锚点 |

## 4. 成本模型（U3：10 人 / 月 10 元内）

**关键在于：索引期不是瓶颈，查询期才是长期成本。**

| 项 | 估算 | 依据 |
|---|---|---|
| 单次全量索引的 embedding 调用 | 976 文件 → 约 3–6k chunks（按 TASK-015A 的 434 文件 / 5760 chunks 外推） | 每 chunk 平均约 150 token |
| **bge-m3 在硅基流动的单价** | **免费**（BAAI 系列在官方价格页为"免费"） | 官方 pricing 页 |
| 增量索引（稳态） | 只嵌变更 chunk | TASK-014 实测增量 1.2s |
| 查询期（10 人日常用） | 每查询 1 次 query embed（94ms） | 用量与 token 都极小 |
| 若要换更强模型 | `Qwen/Qwen3-Embedding-0.6B/4B/8B`（支持 `dimensions` 降维：64–1024，32768 上限） | 官方文档 |

**结论**：按 U3 的预算，**bge-m3（免费）在成本上没有风险**；真正的风险是**限流（TPM/RPM）**——
官方文档列有 429 rate limiting。W6-lane B 的做法（复用现有重试/退避）已覆盖（`api.py` 有 429 + 5xx 重试与
`Retry-After` 处理，上限 30s）；**若未来索引期撞限流**，缓解手段是降 batch / 加 sleep，届时另开卡。

**若将来要付费模型**（用户说"甚至付费模型"）：官方价格页 Embedding 档位为 `¥0.07 / M tokens`（bge 系列之外的档）。
按上表用量，10 人日常**远低于 10 元/月**。**注：价格随时变，落地时以官方 pricing 页为准**。

## 5. 未来任务锚点（U2：本地配置预留，暂不实现）

| 未来任务 | 触发条件 | 需要做什么 |
|---|---|---|
| 本地 ONNX 配置完善 | 用户要"源码不出本机"（隐私）或要脱网运行 | TASK-038（钳制）+ 补 TASK-015A 未完成的截断 A/B（需 bge-m3 本地或云端 GPU） |
| 默认模型从 `local` 切 `api` | 若 `api` 成为长期主路径 | **L3 决策**：改 `DEFAULT` 语义 + 同步 D-44 + 处理已索引块的 D-07 二级失效 |
| 云端 provider 抽象 | 接入第二家（DeepSeek / 自建 vLLM） | `api.py` 已是 OpenAI-compatible，通常只需 registry 加条目 |
| 混合模式（本地兜底 + 云端主） | 云端不可用时降级 | 现有 `api.py` 的失败语义（Auth/RateLimit/Network/Response）已可供上层决策；需要一张"provider 故障切换"卡 |

## 6. W6 之后（方向预告，不在本波执行）

| 里程碑 | 内容 | 前置 |
|---|---|---|
| **M2b-2** | TASK-023 真实数据采集（埋点 + 反馈信号） | W6 完成、用户开始日常使用 |
| **M2b-3** | TASK-050 质量调优（R21/R24/rerank/装填，**必须基于真实数据**） | M2b-2 |
| **M2b-4** | TASK-039（待定）：本地 embedding 完善（若用户需要） | U2 的触发条件 |
| **M2c** | Rust client + 鉴权 + 部署（用户要把服务给朋友用时） | 用户明确需要 |

## 7. 本波验收（编排者收口）

```bash
# 每条泳道完成后：核对报告 → 集成 → 合并
uv run ruff check . && uv run python scripts/check_dependency_direction.py && uv run pytest -o addopts="" -q
# 端到端：新靶场索引 + 检索（云端 embedding） + MCP 冒烟
```

集成通过后更新：`docs/tasks/README.md`（状态与波次表）、
`docs/contracts/PROCESS.md` §3.10（**已登记 R47–R50**：云端主路径 / TASK-038 降级 / TASK-037 靶场替换 / 评测靶场变更）、
本文件的“实际结果”节（实施后追加）。

### 编号说明（避免悬空引用）

- **R44** 已被 TASK-034 的 attach 端点占用（`docs/contracts/openapi.yaml:109`）；
- **R45 / R46 从未分配**——旧任务板的“R44-R46”是编号未同步产生的悬空引用，已在任务板与本节修正；
- 本篇的新裁定从 **R47** 起（R47 云端主路径 / R48 TASK-038 降级 / R49 TASK-037 靶场替换 / R50 评测靶场变更）。
