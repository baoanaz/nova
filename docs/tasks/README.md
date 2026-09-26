# zace 任务板

> **实施 AI 的唯一入口清单。** 状态值：`pending / in_progress / review / done / blocked`。
> 规则：认领与回填流程见 [`../plan/orchestration.md`](../plan/orchestration.md) §2；
> 完成报告模板见同文 §3；契约纪律见 §4。
>
> **2026-09-15 重建**：历史任务卡（TASK-001 ~ TASK-108）已全部归档到
> [`archive/`](archive/)，**不再逐卡维护状态**——它们的代码均已合并进 `main`，
> 逐条判定"代码已合并但卡文头停在 review"的成本高于收益。
> 需要追溯某张卡的设计与偏差时，直接去 `archive/` 找。

## 当前活跃任务（只有这几个）

| 卡 | 标题 | 阶段 | 硬依赖 | 状态 |
|---|---|---|---|---|
| [TASK-109](TASK-109-EvidenceGap二轮补检.md) | **Evidence-Gap 二轮补检**（D-19 落地：候选已索引但没召回） | Phase 5+（质量） | TASK-108 ✅ | pending |
| [TASK-093](TASK-093-真实数据闭环.md) | 真实使用数据采集闭环 | Phase 2（M2b） | TASK-084 ✅ / TASK-091 ✅ | pending |
| [TASK-023](TASK-023-真实场景用例采集.md) | 真实场景用例采集（**由 TASK-093 落地**） | Phase 2 | TASK-040 ✅ | pending |

### 推荐顺序

```text
TASK-109（检索质量，用户点名“下一轮重点”）
   ↓
TASK-093（把真实使用数据接成闭环）
   ↓
TASK-023（随 TASK-093 落地回填，不单独开工）
```

**彼此独立**：109 解决“搜得不够全”，093 解决“量不准”，可并行。
性能/工程化侧的最近三张卡（TASK-114/115/116）已合并，见下方历史索引。

## 已明确不做 / 暂不做

| 项 | 决定 | 理由与出处 |
|---|---|---|
| TASK-050（质量调优） | **不开卡** | 必须基于 TASK-023/093 的真实数据才有意义（HANDOFF §3） |
| Docker Compose 部署 | **不做** | VPS 实际用 `systemd + nginx + 静态产物`；2C4G 内存不足以再叠容器（TASK-092 执行记录、[部署手册](../handbook/deployment/vps.md)） |
| k8s / 多机集群 / 负载均衡 | **V1 不做** | 上限是单机 compose（Module/06 §4-A） |
| CI/CD 自动部署 | **V1 不做** | 手动升级即可，见 [vps.md](../handbook/deployment/vps.md) §9 |
| 数据库外部化 | **不做** | SQLite 单机足够（Module/06 §4-A） |
| 邮箱/短信验证、付费充值 | **不做** | 邀请码已足够；额度固定不可购买（[TASK-110](archive/TASK-110-邀请码与身份分级.md) §8） |
| Cross-Encoder / LLM rerank | **V1.5 再做** | 见 TASK-109 §"明确不做" |
| HyDE / Multi-Query 查询改写 | **不做** | 需额外 LLM 调用，违反 R1 延迟预算 |
| 2-hop 常规图扩展 | **不做** | 只在 G1 触发时按需做（TASK-011 已定） |

## 任务卡模板与惯例

- 新卡：复制 [`TASK-TEMPLATE.md`](TASK-TEMPLATE.md)，按 §1-§5 结构填写
  （背景 / 目标 / 输入 / 设计要点 / 验收标准 / 明确不做 / 执行记录）；
- **文件所有权**：每张卡必须在文头列"交付物所有权"清单，清单外文件不改；
- **完成后**：回填"执行记录"（日期 + 分支 + 验收命令与结果 + 与设计的偏差 + 未决问题）；
- **归档**：合并进 `main` 后，把卡移到 `archive/`，本表删除对应行。

> 本表**只列未完成的卡**。已完成的不在此表——去 `archive/` 找。

## 历史卡索引（按阶段）

| 阶段 | 卡范围 | 位置 |
|---|---|---|
| Phase 0 | 公共底座（P0-1 ~ P0-5） | 无独立卡 |
| Phase 1 | TASK-001 ~ TASK-022 | [`archive/`](archive/) |
| Phase 2 | TASK-030 ~ TASK-052 | [`archive/`](archive/) |
| Phase 3 | TASK-060 ~ TASK-064、TASK-097 | [`archive/`](archive/) |
| Phase 4 | TASK-070 ~ TASK-099 | [`archive/`](archive/) |
| Phase 5+（质量） | TASK-101 ~ TASK-108 | [`archive/`](archive/) |
| Phase 4+（上线加固 / 增长运营 / 可用性） | TASK-110（邀请码与身份分级）、TASK-111（分支身份与内容寻址复用）、TASK-113（LLM 多协议 + 连接自检） | [`archive/`](archive/) |
| Phase 5+（性能 / 工程化） | TASK-114（索引冷启动）、TASK-115（向量阶段多窗口并行）、TASK-116（硬件配置档案）——证据见 `benches/results/index-perf-task{114,115}-vps.md` | [`archive/`](archive/) |

已归档卡的**执行记录里保留了当时的实测证据与设计偏差**，是回溯决策的首选来源。

## 相关文档

- 编排规程与报告模板 → [`../plan/orchestration.md`](../plan/orchestration.md)
- 工作区隔离纪律 → [`../plan/multi-ai-worktrees.md`](../plan/multi-ai-worktrees.md)
- 冻结契约与决策登记 → [`../contracts/PROCESS.md`](../contracts/PROCESS.md)
- 设计意图与决策 → [`../design/INDEX.md`](../design/INDEX.md)
- 操作手册（部署/基准/隐私） → [`../handbook/README.md`](../handbook/README.md)
