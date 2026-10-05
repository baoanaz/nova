# 契约冻结清单（CF）

> 状态：2026-09-10 冻结。维护者：编排者；变更需先在任务卡执行记录里提申请、等裁决。
> 原则：**跨任务的一切接口都是契约**；契约文件之外的实现细节自由。

## 1. 冻结清单

| # | 契约 | 文件 | 设计来源 | 主要消费者 |
|---|---|---|---|---|
| CF-01 | index.db DDL（表/列/索引/语义） | `docs/contracts/index-schema.sql` | Module/01 §3.3 | 001/006/007/010/011 |
| CF-02 | 三种 hash 语义与函数 | `core/zace_core/hashing.py` | Module/01 §2.4、D-43 | 001/007/009；Phase 2 client |
| CF-03 | ContextPack JSON 合同 | `docs/contracts/contextpack.schema.json` | Module/03 §2、D-21 | 012/013；Phase 2/3 service/web |
| CF-04 | 检索候选内部结构（Candidate/Flow/MissingEvidence） | `core/zace_core/types.py` | Module/02 §4、Module/03 §2 | 010/011/012 |
| CF-05 | REST API 形态（路径/方法/关键 payload） | `docs/contracts/openapi.yaml` | Module/06 §2.1、D-37 | Phase 2 service/client/web |
| CF-06 | MCP 工具 schema | `docs/contracts/mcp-tools.json` | Module/05 §2.1、D-12 | Phase 2 client |
| CF-07 | ContextEngine 接口面 | `core/zace_core/interfaces.py` | Module/06 §1、D-34 | 007/013；Phase 2 service |
| CF-08 | 索引侧数据类型（ParsedFile/Symbol/Edge/Chunk/FileDelta） | `core/zace_core/types.py` | Module/01 §2 | 002..007/009 |
| CF-09 | Provider 接口（Embedding/Answer/Parser） | `core/zace_core/interfaces.py` | Module/01/04、D-44 | 002..005/008；Phase 3 |

> CF-09 变更（2026-09-10，L2，编排者）：`EmbeddingProvider` 新增 `embed_query()`——e5 类模型 query/passage 前缀不同，检索侧必须走专用方法；索引侧一律 `embed()`。实现（TASK-008）已含此方法，本次仅补契约；同时统一 `model_id` 命名示例为 `local:<slug>`。

## 2. 跨文件不变式（写代码时不得违反）

1. `chunk_id = {path}:{symbol_fqn}:{start_line}`（spec 块的 `symbol_fqn = heading_path`），代内唯一即可（D-04）。
2. `blob_hash / file content_hash / chunk content_hash` 三者用途互不可替代（D-43 / CF-02）。
3. `symbol_kind ∈ {function, method, class_skeleton, spec_block, fallback_block, ...}`；spec 块双表同 id（见 §3-2）。
4. `evidenceTier` 是元数据 + 装填资格线 + rerank 特征，**不是 ORDER BY**（D-17）。
5. `evidence` 与 `docs` 共用 `E` 编号空间，`flows` 用 `F` 编号（D-21）。
6. RRF `K=60` 且融合层不加通道权重（D-16）。
7. `spec_references.provenance` 永远 `inferred`（D-06）。
8. 所有面向 agent 的时间/预算语义：`freshness` 如实报告，缺证据必须有 `missingEvidence`（D-30、A5）。

## 3. 规划期与实现期裁定（编排者；需在对应 Module 文档下次修订时回记）

### 3.1 规划期裁定（2026-09-10）

1. **chunks_fts 采用独立 FTS5 表（非 external content）**：D-20/D-45 要求索引侧写入预分词文本，与 external-content 直读 `chunks.content` 冲突；写入器与 chunks 同事务维护，渲染一律用原文。DDL 见 CF-01 文件头。
2. **SpecBlock 双表同 id**：检索单元同时写入 `chunks(symbol_kind='spec_block')` 与 `spec_blocks`（结构字段：doctype/heading_path/code_fences）；保证单一 RRF 池、单一向量表、单一 E 编号空间（D-42 的"不新增检索通道"由此落地）。
3. **batch-upload 的 blob 内容编码 = base64（`contentB64`）**：JSON 传输二进制安全；gzip 是否叠加留待 Phase 2 实测（Module/05 §9-2 开放问题不变）。
4. **MCP 工具 description 文案可由 client 任务打磨，但 schema（名字/参数/类型/默认值/上限）冻结**（CF-06 文件头声明）。

### 3.2 实现期裁定（W1 泳道评审，2026-09-10；均已核对代码与测试）

| # | 议题 | 裁定 | 影响 |
|---|---|---|---|
| R1 | `.h` 的 C/C++ 归属 | 默认归 C（TASK-002 registry）；**仓库级策略归 TASK-007 实现**：当仓库已见任意 C++ 扩展名文件（.cc/.cpp/.cxx/.hpp/.hh/.hxx/.ipp/.tpp）时，`.h` 改由 C++ 解析器处理（在 C++ 仓库里 `.h` 语义上就是 C++ 头） | TASK-007 卡片已补口径；V1 已知限制：先索引 `.h` 后出现 `.cpp` 时旧 `.h` 需等下次变更才重解析 |
| R2 | `EmbeddingProvider.embed_query()` | 升格为契约（见 CF-09 说明） | TASK-010 检索侧必须调用；TASK-007 索引侧用 `embed()` |
| R3 | `model_id` 命名 | `local:<slug>` / `api:<model_name>`（落库→改名等于换模型），修订 interfaces.py 示例 | — |
| R4 | `FileDelta` 三集合语义 | 两两不重叠：`new` = hash 新增/变化（需嵌入），`reused` = hash 未变（可复用向量，**复用键是 hash 不是 id**），`removed` = 旧 id 消失（删向量）；下游 upsert/delete 顺序无关 | TASK-007 按此对账 |
| R5 | edges 归属与重挂 | 边按 **source 侧**文件归属（文件变更只清 source ∈ 本文件的边，防误删入边）；符号行号漂移时 spec 引用从旧 id 重挂新 id，fqn 消失才 stale=1 | TASK-006 消费 |
| R6 | C++ MISSING 容忍（TASK-004 偏差申请） | 接受：**仅** 全部错误均为 MISSING 且 ≤5 条时照常抽取（错误入 parse_errors、宏生成声明入 unresolved）；出现任何 ERROR 或超限仍整体 fallback | 仅 cpp.py 内实现 |
| R7 | 函数原型不成符号（TASK-003） | 接受：仅声明的头文件只产出 include-guard 宏；定义处成符号，解析层按 fqn 匹配 | TASK-014 出题时避免只靠声明定位 |
| R8 | imports 边 target_name 形态 | 接受现有形态（如 `.service.Service` / `x.*`）；fqn 化与跨文件匹配算法全部归 TASK-006（含相对导入/包语义） | TASK-006 需要处理该形态 |
| R9 | SpecBlock 结构口径（TASK-005 补充） | 前言块 heading_path=`(preamble)`、level=0；front matter 单独成块；fence 归属最内层 SpecBlock；content 用 splitlines 归一 | TASK-006 切片按此 |
| R10 | 向量相似度语义（TASK-009） | `search()` 返回余弦相似度（越大越相关，按分降序）；`rebuild(dim)` 用 `create_table(mode='overwrite')` 原子替换（LanceDB OSS 无 rename_table）；单写者假设 | TASK-010 直接消费 |

### 3.3 集成期裁定（编排者 E2E 实测发现，2026-09-10）

| # | 议题 | 裁定 | 影响 |
|---|---|---|---|
| R11 | **BM25 多词召回语义**：TASK-001 的 `fts_search` 用 FTS5 隐式 AND，中文自然语言查询（分词后 7+ token）恒零命中 → Vector 单通道 → `answerable` 恒 False | **修复**：`fts_search(..., operator="or")` 为默认（OR 连接、每词引号包裹），`"and"` 保留供高精度调用；同时引入 bm25 列权重 `(content=1.0, signature=5.0, docstring=1.0)`。依据 codegraph `queries.ts:1505-1513`（OR + 列加权）【已验证·快照 2025-09】 | TASK-016；前缀匹配列为 TASK-015 校准项 |
| R12 | **证据块行号非单调**：`_try_merge` 按分数顺序拼接 content、lines 取包围盒 → 渲染出行号回跳（9→6→14→1），误导 agent 对齐编辑；`elidedLines` 语义失真 | **修复**：片段化存储 + 按行序渲染，`elidedLines` = 真实省略行数 | TASK-017 |
| R13 | 跨模块集成回归缺位 | **补齐**：新增 `core/tests/integration/` 承载 M1 级端到端回归（真实 Store+Vector+Indexer+retrieval+contextpack，embedding 用确定性假 provider） | TASK-016 交付该目录；TASK-013/014 在此基础上扩展 |

> 教训（记入流程）：逐模块测试全绿不等于产品可用——R11/R12 均为逐层测试无法暴露的集成缺陷。
> 故 W3 起，任何检索/组装类任务卡的 DoD 必须含跨模块 E2E 断言（已在 TASK-013/014 卡补充）。

### 3.4 自举裁定（TASK-013 自举实测发现，2026-09-10）

| # | 议题 | 裁定 | 影响 |
|---|---|---|---|
| R14 | **兜底切分行号回跳（阻断 M1）**：`fallback.py::_split` 在分隔符分支递归时忽略基偏移（顶层 offset=0 使既有测试全绿）；`uv.lock` 形状的文件产生两个 `{path}:(module):1` → `chunks.id` 主键冲突 → `zace-core ingest --repo .` 在真实仓库直接崩 | **修复**：修根因 + chunk id 唯一性防御（抛带明细的 ValueError，禁静默去重）+ 单文件失败隔离（进 `report.errors` 不中断整次 ingest） | TASK-018（阻断项，优先） |
| R15 | **spec 保底块重复装填**：`reserved not in slots` 用 `_Slot` 对象身份比较（无 `__eq__`）恒为 True → 同一 spec chunk 占两个 E 编号，实测吃掉 ~1.4K token 并挤压代码证据 | **修复**：改按 chunk_id 去重；保底语义与 E 编号=装填顺序不变 | TASK-019 |
| R16 | TASK-016 越界修改 `core/tests/retrieval/test_recall.py` 1 处断言（`channel_ranks` 由 `{inferred:1}` 改为 `{inferred:1, bm25:2}`） | **追认**：属语义变更的必然影响，且未弱化覆盖（仅新增通道）。后续同类情况应在卡内先申请 | — |
| R17 | TASK-013 的 CLI 自举发现：`benches/golden/*.jsonl` 自身在被索引仓库内，负例被查询原文命中 → answerable=True | **TASK-014 出题纪律**：负例须用仓库内不存在的符号/描述，或把 golden 集排除出索引（由 TASK-014 定口径） | TASK-014 |
| R18 | `IngestReport.ambiguous_refs` 在零变更增量里仍报 200（为状态量而非 delta） | **归 TASK-007 口径澄清**（非阻断）：建议改名为 `ambiguous_refs_total` 或在增量里置 0；具体在下次涉及该模块的卡里处理 | — |
| R19 | 跨模块 E2E 测试位置：TASK-016 建了 `core/tests/integration/`，TASK-013 的 E2E 在 `core/tests/cli/` | **保留两者**：`core/tests/integration/` 为跨模块集成测试的规范位置（承载 M1 级断言）；`core/tests/cli/` 保留 CLI 命令行层面的测试。后续跨模块测试统一进 `integration/` | — |

### 3.5 真实靶场实测裁定（编排者在 aibox-super-sdk 上实测，2026-09-10）

首个真实靶场（`aibox-super-sdk`，434 文件 / 5760 chunks，索引耗时 1004s）跑用户种子问题
「workflow 在记忆系统里是怎么定义和使用的？」暴露 BM25 判别力失真：

| # | 议题 | 裁定 | 影响 |
|---|---|---|---|
| R20 | **BM25 判别力问题与噪声处理的因果被实测修正**（编排者与实施 AI 先后误判，最终以实测为准）：① 原假设“标点 `？` 使 `AND` 恒空”**不成立**——FTS5 unicode61 将不可切分标点视为分隔符，`fts_search("workflow ？", operator="and")` 实测 **22 命中**（与不加 `？` 相同）；② 真正使 `AND` 恒空的是**可索引但库外（DF=0）** 的 token（如 `k8s`：`"workflow" "k8s"` → 0，OR → 22）；③ 但 **OR 语义下 DF=0 token 对结果零影响**（实测：含/不含 `？` 的 OR 结果逐项相同），且 **无任何生产调用方使用 `operator="and"`**（`fts_search` 仅被 `recall_bm25` 以默认 OR 调用）；④ IDF 加权重排已实测否决（Σ IDF 后目标 rank 797/148，均不达 ≤10；虚词 `里`/`怎么` 的 IDF 8.25 > 意图词 `workflow` 8.04 ⇒ 稀有度在本语料不可靠） | **TASK-020 收窄为“零成本清理”**：保留纯标点过滤（纯函数、免费）；**删除逐 token DF 探测**（1+11 条 SQL ≈7.4ms/查询）——它在 OR 路径上零收益；`AND` 路径若未来启用再由调用方自备过滤。**排序变更仍归 R24**（需 golden set 基线） | TASK-020（修订中）；R24 待 TASK-014 后评估 |
| R21 | **装填层 Docs/Code 严重失衡**：同一查询最终包 Docs 35 块 / Code 1 块，代码目标被文档淹没 | **升级为头号质量修复**（基线实测 7/18 失败同一模式）。**已核实机制三条**（详见 TASK-021 卡）：① 自然语言对齐偏差（文档双通道命中、代码常单通道）；② 文档切片数放大（同一文件 6 个小节共占 top-8，而装填层只有按文件的 25% 上限、无按证据类型的总量约束）；③ **不是** doctype 加分（实测这些文档为 `guide`，不在 HIGH_VALUE_DOCTYPES，+0.8 未生效——出题时该猜测作废） | TASK-021（code_floor + docs_ratio 互保底） |
| R22 | **负例返回弱证据**：查询「Kubernetes operator 的部署协调逻辑在哪里实现？」（源码 0 命中）返回 11 个文档块且无 `missingEvidence`；基线实测负例通过率 2/6 | **升级为产品级修复**：根因已核实——`_assess` 的 `consensus>=2` 在文档密集仓库上恒为真（高频词命中大量文档，文档天然双通道），导致 `answerable` 永不假，D-24 的诚实短路永不触发 | TASK-022（数据驱动收紧，C1–C4 候选规则实测对比） |
| R23 | 泳道工作区基点差异（如 lane 分支比 main 少 `benches/golden/aibox-seed.jsonl`） | **合并时无差异不是误删**（3-way merge 保留 main 版）；已在合并后校验文件存在 | — |
| R24 | **检索排序的判别力问题尚未解决**（R20 遗留）：OR-sum 天然奖励“命中词多”的块，而本查询的真实意图只集中在 1 个内容词（workflow）；experiment 显示 max-IDF 门控仅能将目标拉到 rank 21，仍不达 ≤10；TASK-020 实测目标 rank 471 与修复前一致 | **不在 TASK-020 内拍脑袋改**：排序属高风险变更，必须先在 golden set（TASK-014）上建基线、再比较候选方案（max-IDF 门控 / 命中数归一 / 长度归一 / rerank 特征）。预修方向入 TASK-015 校准清单 | TASK-014/015；必要时另立卡 |
| R25 | **编排者原 DoD 被数据推翻**：TASK-020 曾要求目标文件进 BM25 top10；实测表明该期望在 5760 chunks + 22 个 workflow 命中块下不现实，且**从检索质量角度，文档优先于单次提及代码是合理行为** | **修正 DoD**：本卡只要求“噪声路径被清理 + 目标 rank 不劣于修复前”；且“代码中唯一提及”类期望需重审（TASK-014 出题时用双层期望：主期望 + 可接受集） | TASK-020 卡已同步修订 |

### 3.6 测试集定位与优化纪律（用户拍板，2026-09-10）

| # | 议题 | 裁定 |
|---|---|---|
| R29 | **当前 60 条 golden set 的定位**：用例由编排者按"人类怎么问"构造，而真实场景下 query 由**另一个 AI 生成**，措辞分布完全不同；且期望答案依赖出题人假设（基线已出现 1 条因期望写错导致的假失败） | **降级为 smoke + 回归护栏**：只用于"改动不破坏既有能力"的门禁，**不作为检索质量优化目标**（避免对着 60 条过拟合，未来全部推翻）。**不作为 CI 硬门禁的部分**：负例通过率（受 R17 与索引范围影响，结构上不可能全过） |
| R30 | **质量参数不得再基于当前测试集调参**：TASK-021 的 `docs_ratio` 经 6 点扫描选定 0.10 才达标；TASK-022 的 `CONSENSUS_SCORE_RATIO=2.15` / `MIN_CONSENSUS_FILES=2` 亦为拟合值 | **已实施的值标注"未经真实数据校准"**（代码 docstring + 本表），冻结不再调参；真实数据到位后统一重评（TASK-023） |
| R31 | **TASK-022 的 L3 偏差裁定**：实现偏离 Module/03 §4.4 条文两处——① 新增 `inferred` 作为 answerable 硬依据（设计原文只有 Explicit；D-15 明确 Inferred 是"召回种子不是精确证据"）；② `consensus>=2` 改为"跨文件共识 + 峰值比"复合条件 | **接受为实现期修订（L3 已登记）**，理由：纯按设计原文（Explicit only）实测正例 answerable 仅 1/54，产品上不可用；但**已知脆弱点**：`_is_corroborated_top`（top-1 被 ≥2 通道命中）在文档密集仓库近乎恒真，仍是负例漏判来源（实测 RabbitMQ 负例因此仍为 True）。**真实数据到位后重评**；TASK-023 采集的负例样本是主要输入 |
| R32 | **优化顺序纪律**（用户指示）：先搭整体（MCP 端到端可用），再基于真实场景优化检索 | Phase 2 优先打通 MCP 闭环；R21/R24/TASK-015-B 等质量调优**推迟到真实数据采集之后**（TASK-023） |

### 3.7 用户提问引出的设计空白（编排者实测确认，2026-09-10）

| # | 议题 | 实测证据 | 裁定 |
|---|---|---|---|
| R26 | **分支切换后的陈旧索引无告警**：project identity（D-29）不含分支名，同一 repo 所有分支共用一个 project（这是对的）；但 `git checkout` 后**未重新同步**则索引仍为旧分支内容，且 `freshness` 报 `fresh` —— 实测：切回 main 后检索仍返回 `on_feature` 的函数，confidence=medium，无任何提示 | 仅切换分支、未编辑文件 → 磁盘内容变了但 content_hash 扫描才发现；**而 D-27 懒同步会在 tool call 时扫描，所以真实客户端下会自动纠正**（实测：重新 ingest 能正确识别 modified）。缺口在 CLI 手查场景。**裁定**：① `files.branch` 列已存在（DDL）但未使用 → 同步时写入分支名，`freshness` 增加“索引分支 ≠ 当前分支”提示；② 列入 Phase 2 client 卡（D-30 freshness 语义的扩展），Phase 1 不修 |
| R27 | **非 git 父目录汇总多仓库**：实测 `/tmp/zace-multi`（非 git 父目录 + 2 个子 git 仓 + 普通文档）→ 父目录 identity 退化为 `sha256(绝对路径)`，**不可跨机器共享**；扫描会把子仓源码全部并入一个巨项目（仅 `.git` 被跳过），子仓单独索引则又是另一个 project（重复工作）；子仓自己的 `.gitignore` 也不生效 | **裁定**：V1 明确不支持“多个 repo 合为一个 project”（Module/01 §6-1 “一 project 一 repo”）；**用户应指向具体仓库路径**。多仓场景留给 Phase 2+ 的 group/workspace 概念（需用户提需求再排期）。**文档层**：在 CLI 输出/错误文案里提示“当前目录不是 git 仓库，身份绑定绝对路径，不跨机器共享” |
| R28 | **同步耗时构成被误读**：用户关心的“每次检索前同步耗时”实测拆解——稳态（0 变更）2.5s 中 **~2.0s 是进程启动（lancedb 导入 1.09s + jieba 词典 0.53s + 其他导入）**，真正扫描仅 0.63s（451 文件，1.4ms/文件） | **Phase 2 架构已自然解决**：client 是常驻 MCP 进程（stdio server 活在整个会话）→ 导入/模型加载只付一次；服务端同样常驻。**仍可优化项**（列入 Phase 2 卡）：① **mtime+size 快路径**（D-28/Module/05 §3.2 已设计但未实现）→ 扫描从 1.4ms/文件降到 ~0.05ms/文件；② lancedb 惰导入；③ `.gitignore` 真实解析（D-28 缺口）减少文件数 |

### 3.8 Phase 2 服务化裁定（编排者，2026-09-10；M2a 开卡时定）

| # | 议题 | 裁定 |
|---|---|---|
| R33 | **service 对 core 的依赖面**：CF-07（`ContextEngine` Protocol）是否为上限？ | **不是上限，是最低保证面**。service 可直接使用 core 的公开类与方法（`zace_core.engine.Engine`，含 `search_with_trace` / `project_dir` / `ingest_repo` / `resolve_repo`）。理由：同 monorepo 同版本演进；D-34 只要求 core 不依赖上层，不限制 service 用 core 的公开 API。**若未来要把 core 换 Rust 实现，再收窄到这个面** |
| R34 | **M2a 鉴权**：Phase 2 本地跑通阶段是否需要 token？ | **不需要**。M2a 为**本地单用户模式**（`ZACE_LOCAL_MODE` 默认 true）：无鉴权、无用户概念、绑定 127.0.0.1。鉴权/token/租户归 M2c（TASK-060/061）。CF-05 的 `/api/auth/*` 路径保留但返回 501（路径契约不漂移，实现待 M2c） |
| R35 | **服务端同步状态存哪**：CF-01（index.db）能否新增表？还是建第二套 DB？ | **都不**。M2a 落**文件**：`{project_dir}/sync-state.json`（路径→blobHash/大小/branch/commit/checkpoints，tmp+replace 原子写）+ `{project_dir}/blobs/{hash[:2]}/{hash}` 内容寻址镜像。理由：不碰 CF-01 冻结 DDL；本地单用户无用户/审计需求；JSON 可调试易备份。M2c 引入 `zace-meta.db`（Module/06 §2.4）时再迁移 |
| R36 | **CF-07 L2 扩展：`ingest` 新增 `source` 参数** | 已由编排者在 main 落地（`interfaces.py`）。**存在的理由**：配置指纹失效（D-07）会走 `full_reparse` / `reembed`，该路径遍历 `source.list_files()` 重建；上传模式下不传 source 会**静默清空索引**。实施归 TASK-031（core 侧加 `source or self._source_for(project_id)`） |
| R37 | **CF-05 扩展：`projectId` 在本地模式可省略** | 本地单用户模式下允许请求不带 `projectId`，服务端使用唯一 local project。理由：降低 client（TASK-040）复杂度，为 demo 服务。**扩展而非破坏**：显式传 `projectId` 时行为不变；仅在 `ZACE_LOCAL_MODE=true` 生效 |

> R36/R37 属 CF-05/CF-07 的**扩展**（新增可选参数/放宽字段必填），已由编排者先改契约文件再放行实现（orchestration §4 的 L2 流程）。

| R38 | **M2a 的 MCP 接入形态**（用户 2026-09-10 拍板）：原设计（Module/05）把 MCP 放在 Rust client，前提是**服务在远端**（client 要扫描本地代码、算 hash、上传 blob）。本地模式下 service 与代码同机同文件系统，该前提不成立 | **M2a 由 service 直接提供 MCP Streamable HTTP 端点**（`/mcp`，编辑器填 URL 直连），同步环节在本地模式整体消失（改为服务端懒重扫 TASK-034 §C）。**Rust client 保留给远端场景**（M2c），届时它才必需（在代码本地扫描/哈希/上传到 VPS）。分期记录：M2a = HTTP MCP；M2c = Rust client + 同步。**依据**：Cursor 官方支持三种 transport（stdio / SSE / Streamable HTTP），HTTP 是其一等公民 |

> R38 是**形态分期**裁定：不推翻 D-39（client = Rust），只是把 Rust client 的实现时点后移到它真正必需的场景。

### 3.9 Phase 2 服务化裁定（续）（编排者，2026-09-10；TASK-035/036/037 开卡时定）

| # | 议题 | 裁定 |
|---|---|---|
| R39 | **CF-05 状态码扩展**：TASK-035 引入 `503 embedding_unavailable` / `503 embedding_unreachable` / `507 storage_error` / `500 index_failed`（CF-05 原文只有 `Error` 信封与 401，未列这些码） | **接受为扩展**（不破坏既有契约：新增状态码 + 既有 `Error` 信封不变）。语义：依赖不可用（503，客户端可重试）与存储故障（507）必须与"请求有问题"（4xx）区分；`index_failed` = 有账本但索引为空（上次索引失败），**不得**再报 `index_in_progress`（那会让客户端无限重试"先同步"） |
| R40 | **本地模式下秘密不进响应**：core 的 `degradedReason` 会把 provider 原始报错（含 `api_key=...`）透传进响应体 | **接受 TASK-035 的修法**：错误响应与 `degradedReason` 统一过 `redact_text`，secret 只进服务端日志。**推广为纪律**：任何把 core/第三方异常文本透给客户端的路径都必须过脱敏 |
| R41 | **TASK-035 未决问题的裁定**：provider 在 embed 阶段失败、但索引非空（`chunks>0`）时，检索返回 **200 + `degraded=true`**（仅 BM25 通道），不返回 503 | **接受**。理由：有部分索引时给出带诚实降级标注的结果，严格优于硬错误；503 只用于"完全无法服务"（空索引 + provider 坏）。**附注（观察项）**：`chunks>0 且 vectors=0`（TASK-031 的静默清空形态）目前**不可见**——该状态应可被探测并如实上报，列入 TASK-036 §D |
| R42 | **D-28 忽略规则在本地模式的落点**：设计把 `.gitignore` 真实解析放在 **client**（Rust `ignore` crate），但 M2a 本地模式**没有 client**（R38），且实测 `DirectorySource` 只用了内置目录名跳过 | **在 core 落地 Python 实现**（TASK-037）：`{repo}/.zaceignore` > `.gitignore`（含否定规则）> 内置默认。**契约是"忽略语义"而不是库**：将来 client 用 `ignore` crate 时，两侧行为须一致（TASK-037 需给出语义清单与对照测试） |
| R43 | **索引范围阈值**（Module/05 §3.1 只对 client 规定了 `>128KB` 跳过与 `>10% 不可打印字符`判二进制） | **同口径下推到 core**（TASK-037）：本地/服务端索引走同一阈值，参数可配置；跳过必须**如实进入 `skipped_files` 并带原因**（D-30）。实测依据：`linux-mtk-hw-hmi` 的 `cmake-build-release/**` 与 `lib/libcv.a`（308MB）当前都会被索引 |

### 3.10 环境切换与云端 embedding 裁定（用户拍板 2026-09-13；W6 开卡时定）

> 背景：用户换开发环境（家里 WSL2）。旧三靶场与 `/tmp` 索引产物均不存在；用户已备好硅基流动 API key，
> 拍板当前开发期全程用云端 embedding。**编号说明**：R44 此前已被 TASK-034 的 attach 端点占用
> （见 `docs/contracts/openapi.yaml:109`），故本节从 **R47** 起编号；R45/R46 **从未分配**，
> 旧任务板里的“R44-R46”是因编号未同步产生的悬空引用，已在任务板修正。

| # | 议题 | 裁定 |
|---|---|---|
| R47 | **当前开发期的 embedding 主路径**：用户在 W6 拍板“全程使用云端免费/付费模型，接受 10 人服务量、月 10 元内；本地 ONNX 暂缓部署（预留接口，以后有空再做）” | **接受为运行期选择，不改代码默认值**：`EmbeddingConfig.mode` 仍为 `"local"`（D-44 的部署形态决策不变），实际运行用环境变量切 `EMBED_MODE=api`。**理由**：把 api 变默认会改 `profile` → 触发 D-07 二级失效（既有索引全部重嵌），且与“源码不出 VPS”的隐私默认相悖；若将来要改默认，走 L3 流程。**预算依据**：BAAI 系列在硅基流动为免费档，付费档 ¥0.07/M tokens 量级（见 `docs/plan/phase2-m2b-w6.md` §4） |
| R48 | **TASK-038（本地 embedding 上限钳制）的处置**：W6 改用云端后，该卡是否还做？ | **降级（deferred）但不撤销**：本地路线暂缓，卡保留、W6 不派活；其 `min` 钳制语义**并入 TASK-046 §B**（API 侧上限默认值修正），使未来补本地配置时直接受益。**触发重评**：用户提出隐私/脱网需求时 |
| R49 | **TASK-037 的对照靶场替换**：原卡要求用 TASK-036 的规模数字（hmi / systemservice / Trellis）做前后对照，但**那些靶场在当前环境不存在** | **更换靶场并明示不可比**：改用 `/home/xuwenzheng/github/hello-agents`（976 有效文件；272 个 >128KB、345 `.png`）
+ `zace` 自身；报告中**必须标注“靶场变更，数字不可与 TASK-036 对比”**。**理由**：诚实报告优于沿用不可复现的旧数字；且新靶场的噪声形态（构建产物/图片/无扩展名）与旧靶场同类，结论仍有效 |
| R50 | **评测靶场变更**：旧主靶场（aibox-super-sdk / linux-mtk-mw-camerasure / linux-mtk-hmi）均不在新环境 | **接受用户指定的 `hello-agents` 为新主靶场**（`4f7682c`），旧 golden **保留不删**但标注“靶场不可得、暂停”，新基线报告与旧报告**不可比**。详见 TASK-047 |
| R51 | **API embedding 的截断与分批缺陷**（编排者 2026-09-13 在新靶场全量索引实测发现）：`local.py` 按 `max_input_tokens` 硬截断，但 **`api.py` 完全没有截断逻辑**（该值只进指纹），且 `iter_batches` 按“条数”而非“token 数”分批 | **接受为阻断级缺陷，归 TASK-046 §D**：实测 `hello-agents` 全量索引失败（1482 文件 / 9971 chunks 已入库、**vectors=0**），182 个 chunk >8192 token（最大 64138）。修法：API 侧按 token 截断（与本地语义一致）+ 按 token 预算分批 + 429 韧性测试。**附注**：失败后掩码状态被检索层**如实报出**（`warning: 向量索引为空（可能未重建）：chunks=9971，vectors=0`）——R41 要的“可探测”已实现，不必另修 |
| R52 | **embedding 来源的切换与配置化**（用户 2026-09-13 拍板）：当前开发期用 Voyage `voyage-4-lite`（200M 免费 token；RPM 2000 / TPM 1600 万），额度用完可切硅基流动或其他 | **接受，并立 TASK-049 做架构整理**：批大小/批 token 预算/并发度/超时/重试/上下文上限均**配置化**且**可按模型给不同值**（三级回落 env > 模型 > 厂商 > 全局）；厂商差异（认证/请求体/响应/错误字段）收进 `ApiTransportSpec`，新增厂商只加 spec。**并发默认 1**（串行）；免费档限流脆弱，并发由用户显式开启 |
| R53 | **性能目标口径**（用户 2026-09-13 指示）：Voyage 配额 TPM 1600 万，要求“尽量大、用满 80% 官方性能” | **接受为参数选定目标（≈12.8M tok/min）**：但实测单请求吞吐与批上限构成瓶颈（单批 ≤1000 条 / ≤320K token），需**大批 + 并发**组合才能逼近；具体默认值由 TASK-049 **实测标定**，**不得写入未经实测的数字**；并保留“限流时自动退避、不硬刷”的纪律 |

## 4. 契约的验证方式（集成保障）

- CF-01：TASK-001 的测试必须真实执行该 SQL 建库；DDL 与测试一起通过才算契约落地。
- CF-03：TASK-012 必须对产出 JSON 做 schema 校验（`jsonschema` 校验器进 core dev 依赖，TASK-012 卡内落地）。
- CF-04/CF-07/CF-08：Python 类型层面由导入即用保证；签名变化即 CI 失败（下游测试无法通过）。
- CF-05/CF-06：Phase 2 service/client 各自对文件做一致性测试（服务端路由快照测试 / client schema 校验）。
- 契约文件与设计文档冲突时：以本清单行 + 契约文件为准，由编排者裁定并同步设计文档（§3 记录为例）。
