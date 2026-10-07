# zace 文档中心

[返回项目首页](../README.md)

第一次使用建议按 **本地部署 → MCP 接入 → 架构介绍** 阅读；准备改代码时，再查设计、契约和贡献指南。

## 入门与学习

| 文档 | 解决的问题 |
|---|---|
| [本地部署](handbook/deployment/local.md) | 安装依赖、配置模型、启动服务与 UI、初始化账户、接入 Agent |
| [直接使用 core](handbook/getting-started/README.md) | 不启动 Web，使用 CLI 建索引和检索 |
| [npm 客户端使用](../npm/README.md) | 安装 `zace-client`，配置 Codex、Claude Code、Cursor、pi |
| [npm 包学习](handbook/getting-started/npm-client.md) | 从启动器到 Rust 二进制，理解六个平台包和发布流程 |
| [架构介绍](architecture/README.md) | 模块职责、数据流、检索与问答的边界、源码阅读路线 |
| [Web 开发与品牌](../web/README.md) | 前端页面、开发代理、主题和显示名称配置 |
| [开发环境清单](../ENVIRONMENT.txt) | 工具版本、依赖安装、私有配置和检查命令 |

## 部署与运维

| 文档 | 内容 |
|---|---|
| [VPS 部署](handbook/deployment/vps.md) | 完整服务的公网部署 |
| [WSL 同构环境](handbook/deployment/wsl-live.md) | 在本机复现反向代理与服务部署 |
| [UI 模拟演示](../web/demo/README.md) | 单独运行模拟接口和静态 UI |
| [版本更新](handbook/release/README.md) | 更新代码、重建前端和重启服务 |
| [npm 发布手册](handbook/release/npm.md) | 维护者发布新版本的操作流程 |
| [配置与隐私](handbook/privacy/资产清单.md) | 环境变量、密钥位置与迁移 |
| [请求日志与 trace id](handbook/operations/请求日志与trace-id报错手册.md) | 定位一次失败调用 |
| [索引白名单](handbook/operations/索引白名单.md) | 按需纳入被 `.gitignore` 忽略的文档 |

## 评测与开发

| 文档 | 内容 |
|---|---|
| [Benchmark 方案](handbook/benchmark/README.md) | 质量、性能、问答评估的入口和报告约定 |
| [公共靶场与用例](../benches/README.md) | 固定输入、runner 参数、golden 用例和指标 |
| [新机器性能复测](handbook/perf/README.md) | 本地处理、网络、embedding API 与冷启动 |
| [历史报告](../benches/results/README.md) | 与设备、版本及配置绑定的测量结果 |
| [贡献指南](../CONTRIBUTING.md) | 分支、PR、模块检查和文档维护 |
| [设计索引](design/INDEX.md) | 详细设计与决策来源 |
| [契约变更流程](contracts/PROCESS.md) | API、MCP 和数据结构的一致性约束 |
| [路线图](plan/roadmap.md) | 后续工作方向 |
| [任务索引](tasks/README.md) | 活跃任务与实施记录 |
| [开发交接](../HANDOFF.md) | 当前开发上下文；具体状态以对应日期和代码为准 |

## 文档与资源放在哪里

```text
docs/
├── README.md       本入口
├── assets/         项目 Logo、对外展示的当前 UI 截图
├── architecture/   面向新贡献者的架构说明
├── handbook/       可执行的安装、部署、评测、发布、运维步骤
├── design/         详细设计、决策与背景研究
├── contracts/      接口与数据契约
├── plan/           路线图和方案
├── tasks/          开发任务、完成记录
├── evidence/       专项验证证据
└── archive/        历史材料
```

图片约定见 [assets/README.md](assets/README.md)。公共评测用例与报告继续放在 `benches/`，运行数据和临时输出放在被忽略的 `.local/`。

新增文档应加入本页或对应专题索引。详细操作只维护一份主要说明，其他入口链接到它；移动已有文档时保留旧路径的跳转说明。历史材料注明适用时间，当前使用方法以入门指南、操作手册及对应实现为准。
