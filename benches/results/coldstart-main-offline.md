# 冷启动主线离线优化与 WAL A 对照

日期：2026-10-11。范围：服务已就绪、未索引 LangChain 的真实 stdio MCP `search_context`。
本轮不调用真实 embedding API，不执行 Nova 仓库冷索引，不纳入并行客户端/服务/Web 改动。

## 结论

- 保留尾窗提前派发、词法/向量召回重叠、persist 分段计时和免费延迟回放。
- **没有证明普通回放有可靠整体提速，也没有完成稳定低于 30 秒的正式验收。**
- WAL A（每 2 秒后台 `fdatasync`）有小幅正向信号，**不默认启用、不进入生产路径**。
- 预取代码指纹、闲置回收、有界关停等边界硬化继续暂缓；未通过的试作已撤回。
- 最终仅补必要正确性：真实 COMMIT 失败回滚；召回保持原向量执行超时守卫及错误封装。

## 最终验证

最终隔离源码回归：**303 passed、7 skipped、4 个既有 xfailed**，Ruff 全部通过。
真正 COMMIT 故障/成功重试、新连接可见性，以及原向量超时守卫均有针对性测试。
早期失败运行日志保留；临时运行器补 __main__ 保护后全部通过，以 regression-final2.txt 为准。
生产代码复审的 COMMIT 阻塞已解决；最后只复核成功记录与报告链接，不再扩展实现。

## 完整 MCP 测量

起点：初始化、tools/list、事前 metrics 完成后，主机编码/发送 tools/call 前。
终点：完整匹配 id=1 的 JSON-RPC 结果收齐并 JSON 解析完成。
不含服务准备、MCP 初始化、退出、事后 metrics/内容验证。

同一冻结 release 客户端、同一 e339464 服务源码；候选只覆盖本轮 core 三文件。
每次独立进程/空存储/空客户端缓存、独立 systemd cgroup：
MemoryHigh=1200M、MemoryMax=1500M、MemorySwapMax=256M、TasksMax=128，
IPAddressDeny=any / IPAddressAllow=localhost。
fixture 与 Git 跟踪语料的 fadvise DONTNEED 是建议，不保证物理冷盘一致。
直接使用不可变 ReplayEmbedding，未命中本地失败，无网络 fallback。
网络属性是配置证据，不伪造 paid_calls/network_calls 观测计数。

### 普通回放：AB、BA、AB 三组

| 组 | 基线 / 秒 | 候选 / 秒 |
|---|---:|---:|
| 1 | 46.801907 | 34.762408 |
| 2 | 28.107804 | 28.744226 |
| 3 | 28.871733 | 26.166167 |
| 中位数 | **28.871733** | **28.744226** |

中位数差 0.127506 秒（约 0.44%），不足以认定可靠收益。
control-1 / candidate-1 / candidate-2 的 swap 峰值分别约 246.3 / 119.4 / 134.3 MiB；
保留所有样本，不挑掉慢样本制造收益，不与此前 26.924648 秒跨环境直接比较。
候选普通三次中有一次超过 30 秒；不能宣布稳定达标。

### 延迟回放：探索性单对，非在线实测

passage 模型：500 条/批、每消费者最多 4 批并发，按批波次注入固定延迟；
原两消费者可并行，不模拟真实 token 配额、重试、HTTP CPU、WAN 上传或 provider 尾延迟。
query 独立注入 0.5 秒。没有真实远程调用。

| 批波次延迟 | 基线 / 秒 | 候选 / 秒 | 观察 |
|---|---:|---:|---|
| 1.5 秒 | 27.099404 | 27.178282 | 近似持平 |
| 3.0 秒 | 29.563299 | 28.845821 | 候选快 0.717478 秒 |

每档仅一对，不能作线上收益或统计验收。
尾窗原本已与图处理重叠，并不是所有批延迟都能再从总时延中扣除。

## WAL A 单变量实验

双方使用同一个候选快照、同样的提交前观测。
A-on 只在已有 WAL 上后台 fdatasync，不写数据、不调用 SQLite、不调整事务、
synchronous、自动 checkpoint，也不把后台同步移到 Tool 返回之后。
背景工作在批出口排空，所有结果等价。

| 组 | A-off / 秒 | A-on / 秒 | 节省 / 秒 |
|---|---:|---:|---:|
| 1 | 26.133555 | 25.943946 | 0.189609 |
| 2 | 26.567888 | 25.432111 | 1.135777 |
| 3 | 26.873355 | 26.164116 | 0.709240 |
| 中位数 | **26.567888** | **25.943946** | **0.623942（2.35%）** |

- 批事务中位数：17.868955 → 17.299954 秒。
- 事务出口中位数：5.322009 → 4.540625 秒。
- A-off / on 最大 cgroup RAM 峰值：1082.86 / 1145.91 MiB；仍有内存余量风险。
- 三对均更快，但样本数少、收益小；不足以默认上线。不要把 COMMIT 差额当成全部 Tool 收益。
- 提交前实际 WAL：**94,335,672 字节，约 89.97 MiB**，六次相同。
  此值不是旧诊断累计约 191 MB 的 WAL 读取请求量。
- 本次在活动写事务、提交前采样：journal_mode=wal、synchronous=1（NORMAL）、
  wal_autocheckpoint=1000、page_size=4096、cache_size=-2000。
- B（延后 checkpoint）、C（分块提交）未实施；没有降低持久性策略。

## persist 主线定位

以下是一个无 swap 的真实样本 candidate-3，不拼接中位数。
完整 Tool 26.166167 秒；persist 6.8432 秒，CPU 5.3353 秒。

| 子阶段 | 累计墙钟 / 秒 | CPU / 秒 |
|---|---:|---:|
| persist_reconcile | 0.8152 | 0.4642 |
| persist_chunks | 1.0488 | 0.8437 |
| persist_fts | 0.8737 | 0.7136 |
| persist_symbols | 1.1769 | 0.9784 |
| persist_specs | 0.0783 | 0.0598 |
| persist_edges | 1.7429 | 1.5100 |
| persist_unresolved | 0.0724 | 0.0620 |

七子阶段合计约 5.8082 秒，剩余约 1.035 秒含校验、事务/保存点和包装开销。
子阶段已嵌套于 persist，不能再加到整个 Tool。跨向量消费者的 vec_* 是累计观测，
也不能当成串行阶段相加。未来优先看 edges、symbols 的写入成本及 graph_edges；
本轮不继续做结构重构或 SQL/schema 改造。

## 正确性与版本边界

16 个样本均：2986 文件、20931 chunks/FTS/向量、15947 symbols、72080 edges，
960 spec_blocks、15768 spec_references、538 unresolved_refs，无 ingest errors。
SQLite integrity、向量维度 1024/有限值及逻辑数据/FTS/向量/规范化 Tool 指纹一致。
等价性仅剔除 indexed_at、内部 unresolved surrogate IDs、fresh elapsed-age 显示；
不声称原始数据库文件、向量物理文件或原始 Tool 字节相同。
实际结果 jsonrpc=2.0 / id=1 / 正常 result。

已测候选 patch：
`9426244ae30ee8e8598c3a5280f7fe69b55a79655eb73780fc61d260b26f48f3`。
最终 core patch：
`28fed63a085d047e1b6b4eb0ab1ac23a2ba328ff549c2da37826c89187942cab`。
最后修复 COMMIT 异常保护和恢复原向量执行超时守卫后，**不追加大型性能轮次**；
以最终隔离回归、定向故障测试与复审验证正确性，不能将这两项修复宣称为新的提速。
旧 rollback 测试仅模拟事务体失败；新增测试在 execute(COMMIT) 处失败且保留活动事务，
验证失败后事务已结束、同一 Store 重试后新连接可见已提交数据。

本轮没有重建/覆盖原始硬链接客户端。固定二进制 SHA256：
`1e516a0f0710d4150fbfa486968dd74b112c2685b0a111a06fa48351e69310da`。
语料 commit：`e75dae1f53c99c2b5ddb0c7bb36022c6aea25569`。

## 仓库规模与 Nova 冷启动估算（未执行）

此前本轮只读 Git 跟踪文件统计，不是实际索引清单，亦未纳入此后并行改动：

| 仓库 | 跟踪文件 | 总大小 | ≤128KiB 常见文本候选 | 候选大小 |
|---|---:|---:|---:|---:|
| LangChain | 3165 | 41.78 MiB | 2852 | 13.42 MiB |
| Nova | 671 | 13.43 MiB | 594 | 5.86 MiB |

LangChain 是中大型 Python 仓库，若二选一偏“大型”，不是超大型 monorepo；
Nova 是中小型仓库。规模分类是工程相对判断，不是统一行业标准。
Nova 240 个 .py / 187 个 .md，LangChain 2611 个 .py，不能只按文件数线性缩放。

Nova 文本候选体积约为 LangChain 的 44%。粗模型保留约 3–4 秒固定开销，
其余按工作量缩放，中心落在 14–15 秒；考虑多语言、中文分词、图密度、
磁盘和内存压力，给出 **约 12–20 秒的离线等效冷启动估算**。
这不意味着已有 Nova fixture，也不是 Nova 冷索引实测；不保证低于 20 秒。
真实 API 版本还需网络、实际 batch/token 配额、provider 延迟与重试条件，
目前不提供伪精确的在线耗时或费用结论。获得授权后再实测。

## 文件与证据

生产改动：
- core/nova_core/pipeline/indexer.py
- core/nova_core/retrieval/__init__.py
- core/nova_core/storage/store.py
- core/nova_core/storage/db.py

基准入口：
- benches/embed-bench/replay.py
- benches/embed-bench/sync_probe.py
- benches/embed-bench/acceptance_suite.py

[汇总与逐样本阶段](../../.local/coldstart-next/summary.json)；
[首轮 review](../../.local/coldstart-next/review.txt)；
[交付 review](../../.local/coldstart-next/review-delivery.txt)；
[最终回归](../../.local/coldstart-next/regression-final2.txt)。
原始样本在 `.local/coldstart-next/` 各标签目录；临时 index/client-cache 已逐样本审查链接后清理。
试验脚本与冻结运行快照归档至 backups，活动脚本在收尾时清理。
