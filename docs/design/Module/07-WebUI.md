# Module 07 — WebUI（组件详细设计）

> 系列：nova 组件详细设计（Module/），本文是第 7 篇，消费层。
> 定位按用户指示：**只保留对接思想与页面骨架，视觉画风、组件库、设计稿全部留白**，后续另立文档。
> 依赖：全部能力来自 nova-service 的 REST API（Module/06 §2.1）；不直连 core、不碰数据目录。
> 状态：骨架（画风待定）。最后更新：2025-09-09。

## 0. 组件定位

一句话：**nova 的管理面与调试面，不是分析工作台**。

V1 的 web 只回答四类问题：项目索引好了吗、同步到哪了、检索效果如何（playground）、token/用量怎么管。它**不是** Graph 可视化工具，不是 IDE，也不是另一个 Cursor。

```text
nova-web（SPA，静态托管）
   │ REST（session cookie）
   ▼
nova-service（Module/06 §2.1 的全部端点）
```

## 1. V1 页面骨架（Task.md §13 兑现，逐页对接）

| 页面 | 内容 | 对接 API（Module/06 §2.1） |
|---|---|---|
| Login | 登录；注册入口（按配置显隐） | POST /api/auth/login、/register |
| Project List | 我的项目：名称/最后同步/索引状态/文件数 | GET /api/projects |
| Project Detail | 索引统计（files/chunks/symbols/edges）、sync 状态、索引 job 进度、删除项目（二次确认） | GET /api/projects/:id、/api/sync/status/:id、DELETE |
| Search Playground | 输入 query → 展示 ContextPack 的 Markdown（evidence/flows/docs/missing 分节渲染）+ budget/confidence | POST /api/query/search |
| ask_project Playground | 输入 question → answer + 引用列表 + 耗时 | POST /api/query/ask |
| Tokens | API token 创建/列表/撤销（明文仅创建时显示一次） | POST/GET/DELETE /api/auth/tokens |
| Usage | 查询审计：次数/confidence 分布/citationCoverage（04 §8 数据的消费口） | GET /api/usage/projects/:id |
| Settings | ANSWER_* LLM 配置状态、register 开关展示（只读，改配置走环境变量） | service 扩展的只读端点 |

## 2. 对接原则（本文真正要定型的部分）

1. **web 无秘密**：只持 session cookie；API token 只在创建瞬间经手，不落 localStorage 长存。
2. **Playground 是第一优先页面**：它是 02/03/04 质量调试的唯一人工入口——检索效果回归（golden set 之外）靠它目测；渲染直接复用 search 接口返回的 Markdown（服务端渲染，D-21），web 只做分节包装，**不重新实现 ContextPack 渲染**（杜绝双层渲染漂移）。
3. **状态即数据**：索引 job 进度、sync 状态全部来自 API 实况，web 不做推算/缓存——freshness 语义（D-30）在 UI 上如实呈现"索引中 N 文件"而不是假装完成。
4. **删除即删除**：项目删除的二次确认文案必须写明"全部源码镜像与索引将永久删除"（Task.md §15 的透明度要求）。

## 3. 技术形态（只定形，不定画风）

- 静态 SPA（任意主流框架皆可，Vite 构建），Caddy 托管（Module/06 §4-A compose 已含）
- 与 service 同仓（web/ 目录，D-35 monorepo）
- 无 SSR、无 BFF 层——纯静态直连 /api

## 4. V2+ 候选（记录想法，不承诺）

Retrieval Trace（查询为何命中：RRF 通道贡献/rerank 特征）、ContextPack Inspector（预算装填可视化）、Graph/Call Flow 可视化、Ranking Debugger——全部依赖 02 的诊断数据暴露程度，等 golden set 工作成熟后再议。

## 5. 决策登记

D-40 WebUI V1 = 管理面 + Playground（服务端渲染复用，不自研渲染层）；Graph 可视化等分析面能力全部 V2+。
