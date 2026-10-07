# core 快速开始

[文档中心](../../README.md) · [完整服务与 UI 部署](../deployment/local.md)

本篇直接使用 `nova-core` CLI 对仓库建索引和检索，不启动 Web 或账户服务。想配置 UI、API Key 和 MCP，请阅读完整部署指南。

## 1. 准备环境

需要 Python 3.12+、uv 和 Git。只有运行 Web 或 npm 客户端时才需要 Node.js；从源码编译客户端时才需要 Rust。

```bash
git clone https://github.com/baoanaz/nova.git nova
cd nova
bash scripts/setup-dev.sh
```

安装脚本使用锁定依赖，并创建不含实际密钥的本机配置。详细环境清单见 [ENVIRONMENT.txt](../../../ENVIRONMENT.txt)。

## 2. 配置模型

在仓库外的 `$HOME/.key/nova/secrets.env` 中填写真实 `EMBED_API_KEY`。当前 `.env.example` 默认使用 Voyage API、`voyage-4-lite`、1024 维；真实向量索引需要有效的模型配置。

从仓库根目录加载环境：

```bash
set -a
source .env
set +a
```

API 模式会把相应代码或文档文本发送给配置的 embedding 服务。Mock embedding 只用于测试，不能把它当作实际检索质量的依据。切换模型和数据指纹的约定见 [配置切换手册](../operations/embedding-provider切换.md)。

## 3. 建索引与查询

```bash
uv run nova-core ingest --repo /绝对路径/你的仓库 --data .local/core-demo
uv run nova-core search --data .local/core-demo "你的代码问题"
```

第一条命令完成仓库解析与索引，第二条命令对同一数据根查询。`--data` 决定这组索引的位置，后续操作应使用对应目录；检索选项可通过 `uv run nova-core search --help` 查看。

core CLI 适合直接验证索引与检索，完整 Agent 场景使用 `nova-service` 与 npm 客户端。

## 4. 下一步

| 目标 | 文档 |
|---|---|
| 启动服务、UI 和 MCP | [本地部署](../deployment/local.md) |
| 理解索引与检索链路 | [架构介绍](../../architecture/README.md) |
| 跑固定靶场与质量回归 | [Benchmark 方案](../benchmark/README.md) |
| 提交 core 改动 | [贡献指南](../../../CONTRIBUTING.md) |

若索引失败，先检查加载的凭据、模型地址和供应商返回的错误；切勿将真实 Key 放入 Issue 或评测报告。
