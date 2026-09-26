# zace golden eval 报告

- golden：benches/golden/HelloAgents/HelloAgents.jsonl（20 条用例）
- repo：(未绑定仓库：--project-id 模式)
- project：5143d88f059e6c0d
- 生成时间：2026-09-26 08:49:17
- 预算：maxTokens=10000｜排名口径=ContextPack 装填序｜top-k=10
- 向量通道降级用例数：0

## 总体（正例）

| 指标 | 值 |
|---|---|
| 正例数 | 19 |
| recall@5 | 0.842 |
| recall@10 | 0.947 |
| MRR | 0.765 |
| 负例通过 | 1/1 |

## 按语言（lang）

| 分组 | 用例数 | recall@5 | recall@10 | MRR |
|---|---|---|---|---|
| en | 4 | 1.000 | 1.000 | 0.875 |
| mixed | 5 | 0.800 | 0.800 | 0.800 |
| zh | 10 | 0.800 | 1.000 | 0.704 |

## 按类别（category）

| 分组 | 用例数 | recall@5 | recall@10 | MRR |
|---|---|---|---|---|
| behavior | 15 | 0.800 | 0.933 | 0.781 |
| path | 1 | 1.000 | 1.000 | 0.500 |
| symbol | 3 | 1.000 | 1.000 | 0.778 |

## 失败清单（正例）

| id | lang | category | query | 期望 | top-3 |
|---|---|---|---|---|---|
| H-19 | mixed | behavior | 文件工具靠哪个 field 做 read-before-write 的 optimistic conflict detection？ | hello_agents/tools/builtin/file_tools.py#ReadTool | examples/file_tools_demo.py:70-124 (demo_optimistic_locking) / docs/file_tools.md:160-208 (HelloAgents 文件操作工具使用指南 > 乐观锁机制 > 工作原理) / docs/file_tools.md:243-275 (HelloAgents 文件操作工具使用指南 > 使用示例 > 示例 2：乐观锁冲突检测) |

## 负例清单

| id | 通过 | query | answerable | missingEvidence |
|---|---|---|---|---|
| H-20 | 是 | HelloAgents 里向量检索（Qdrant 向量库）的实现代码在哪个文件？ | False | unresolved_reference, retrieval_truncated |
