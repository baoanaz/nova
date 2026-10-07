# zace

zace 是面向 Coding Agent 的代码库上下文引擎。它结合代码解析、符号图谱、全文与向量检索，
为开发、调试和代码审查提供带文件位置的上下文，减少 Agent 反复搜索、读取和整理代码的成本。

通过 MCP 暴露两个工具：

- **search_context**：检索并组装上下文，不调用 LLM。
- **ask_project**：在检索证据上调用可配置的 LLM 生成回答，并回验引用；缺少配置或证据时明确降级。

当前解析器支持 Python、C/C++ 和 Markdown。后端为 Python，stdio MCP 客户端为 Rust，
管理界面使用 React。外部 embedding/LLM 服务可配置；API 模式会将相应文本发送给配置的服务，
本地 ONNX 模式的安装和性能要求见操作手册。

## 开始使用

客户端连接已有服务：

```bash
npx zace-client --base-url <服务地址> --token <API-Key>
```

接入方式见 [客户端说明](npm/README.md) 与
[Agent 接入手册](docs/handbook/getting-started/agent接入与API-Key.md)。

从源码开发需要 Python 3.12+、uv；Web 另需 Node.js 22 和 npm：

```bash
git clone https://github.com/baoanaz/zace.git
cd zace
bash scripts/setup-dev.sh --with-web
set -a; source .env; set +a
```

安装清单、Rust 工具链和验证命令集中在 [ENVIRONMENT.txt](ENVIRONMENT.txt)。
安装脚本只创建空凭据配置；真实 API 索引前，在本机
`$HOME/.key/zace/secrets.env` 填入供应商签发的 `EMBED_API_KEY`。
本地 `.env` 加载该文件；密钥不放在仓库中。

运行带账户鉴权的开发服务：

```bash
uv run zace-service serve --host 127.0.0.1 --port 8787
```

前端开发、首次账户初始化与生产部署见 [操作手册](docs/handbook/README.md)。
当前仓库不预置可用的公共服务地址或生产账户。

## 仓库构成

| 路径 | 职责 |
|---|---|
| `core/` | 代码解析、切片、SQLite/FTS、向量、图谱、检索与上下文组装 |
| `service/` | REST/MCP、鉴权、租户、同步、索引任务、审计与 LLM 总结 |
| `client/` | Rust stdio MCP 客户端与本地同步代理 |
| `web/` | React/Vite 管理界面 |
| `npm/` | 客户端 npm 启动器与平台包清单 |
| `configs/` | 无密钥的硬件配置档案 |
| `scripts/` | 环境安装、基准入口、发布与一致性检查 |
| `benches/` | 公共基准用例、计量工具和设备绑定的测试报告 |
| `docs/handbook/` | 安装、部署、基准、隐私与运维指南 |
| `docs/design/`、`docs/contracts/` | 架构决策与冻结契约 |
| `docs/tasks/`、`docs/plan/` | 开发任务与后续计划 |

依赖方向为 `web, client → service → core`；CI 检查 core 不依赖上层模块。
源码、依赖锁文件、说明与脱敏报告可提交；`.venv/`、`.env`、`.local/`、
构建产物、索引、日志和 `backups/` 被 Git 忽略。

## 性能与验证

换机复测使用固定版本的 LangChain，并保持模型、维度、批大小和并发一致：

```bash
bash scripts/benchmark-vps.sh local <langchain路径>  # 无 API 请求
bash scripts/benchmark-vps.sh hardware               # CPU 与直连网络
```

脚本默认把结果写入 `.local/bench/`。真实 API 吞吐和完整冷启动需要 Voyage 凭据并消耗配额。
测试版本、旧基线、内存限制和指标定义见 [性能手册](docs/handbook/perf/README.md)。
已有索引仅适用于对应机器和配置，换机后不能假定它仍然存在。

开发与提交约定见 [CONTRIBUTING.md](CONTRIBUTING.md)；
开发现状见 [HANDOFF.md](HANDOFF.md)；
安全问题报告见 [SECURITY.md](SECURITY.md)。

## 许可证

[MIT OR Apache-2.0](LICENSE)，与 Rust 客户端及 npm 包已有的许可声明一致。
第三方依赖和外部基准仓库保留各自许可证。
