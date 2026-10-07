# nova 操作手册

[文档中心](../README.md) · [项目首页](../../README.md)

本目录集中维护安装、接入、部署、评测和运维步骤。第一次使用请从 [本地部署指南](deployment/local.md) 开始。

## 按目标查找

| 目标 | 入口 |
|---|---|
| **本地运行完整服务和 UI** | [本地部署](deployment/local.md) |
| 直接使用 core CLI 建索引、检索 | [core 快速开始](getting-started/README.md) |
| 安装系统工具与开发依赖 | [环境清单](../../ENVIRONMENT.txt) |
| 连接 Agent / 编辑器 | [npm 客户端说明](../../npm/README.md) · [接入与 API Key 手册](getting-started/agent接入与API-Key.md) |
| **学习 npm 包结构与启动原理** | [npm 包学习](getting-started/npm-client.md) |
| 理解模块与数据流 | [架构介绍](../architecture/README.md) |
| 部署到公网 | [VPS 部署](deployment/vps.md) |
| 本地验证生产同构部署 | [WSL 环境](deployment/wsl-live.md) |
| 只展示模拟 UI | [UI 演示部署](../../web/demo/README.md) |
| 更新服务与 UI | [发布与更新](release/README.md) |
| 发布 npm 客户端 | [npm 发布手册](release/npm.md) |
| **评估检索、性能和问答** | [Benchmark 方案](benchmark/README.md) |
| 新机器性能复测 | [性能手册](perf/README.md) · [执行提示词](perf/PROMPT.md) |
| 管理设备配置档案 | [硬件档案](../../configs/profiles/README.md) |
| 管理私有凭据 | [资产清单](privacy/资产清单.md) |
| 切换 embedding 配置 | [provider 切换](operations/embedding-provider切换.md) |
| 控制索引范围 | [索引白名单](operations/索引白名单.md) |
| 定位失败请求 | [日志与 trace id](operations/请求日志与trace-id报错手册.md) |

## 目录

```text
handbook/
├── getting-started/  core 入门、MCP 接入、npm 包学习
├── deployment/       本地开发、VPS、WSL
├── benchmark/        质量、性能与问答评估方案
├── perf/             硬件与真实 API 冷启动复测
├── release/          版本更新与 npm 发布
├── operations/       参数、日志、索引范围与排障
└── privacy/          配置、凭据与迁移
```

## 历史内容与兼容入口

| 内容 | 说明 |
|---|---|
| [部署指南.md](部署指南.md) | 早期合集的跳转入口，保留旧链接 |
| [M2a 验收手册](getting-started/M2a-验收手册.md) | 阶段验收记录，包含历史形态；当前部署优先看本地/VPS 指南 |
| [云端 embedding 入门](getting-started/cloud-embedding.md) | 部分 provider 示例来自旧配置，当前默认参数见 `.env.example` |
| `docs/archive/`、`docs/evidence/` | 历史截图、实施证据与专项报告，保留各自日期和上下文 |

设计、契约、路线图和任务的入口统一在 [文档中心](../README.md)。更新手册时同步相关索引；移动旧文档时留下跳转页，避免已有链接失效。
