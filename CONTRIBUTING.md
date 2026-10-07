# 如何贡献

[项目首页](README.md) · [文档中心](docs/README.md)

欢迎提交可复现问题、改进建议、文档勘误、代码修复和公共 benchmark 用例。安全问题请按 [SECURITY.md](SECURITY.md) 报告。

## 开始一次贡献

1. 对需要讨论的功能或行为变更，先在 Issue 中说明目标和具体场景。
2. Fork 仓库，创建主题分支，例如 `docs/local-setup` 或 `fix/client-startup`。
3. 围绕一个问题修改，运行相关检查，更新对应文档。
4. 提交 PR，说明问题、改后行为、验证方式，以及仍未覆盖的情况。

简单文档勘误可以直接提交 PR，无需先开 Issue。提交信息和说明清楚表达修改目的即可。

## 安装开发环境

在仓库根目录运行：

```bash
bash scripts/setup-dev.sh --with-web
set -a
source .env
set +a
```

完整步骤见 [本地部署](docs/handbook/deployment/local.md)，工具版本和 Rust 安装见 [ENVIRONMENT.txt](ENVIRONMENT.txt)。了解代码从 [架构介绍](docs/architecture/README.md) 开始，进一步设计与开发上下文见 [设计索引](docs/design/INDEX.md) 和 [HANDOFF.md](HANDOFF.md)。

## 按模块修改与验证

| 改动范围 | 主要内容 | 对应验证 |
|---|---|---|
| `core/` | 解析、切片、存储、检索、上下文组装 | Python 检查；检索改动增加质量回归 |
| `service/` | API、账户、同步、索引任务、LLM | Python 检查与受影响的接口测试 |
| `client/` | Rust stdio 客户端、同步代理 | Rust 测试；涉及发行时检查平台包一致性 |
| `web/` | React UI | lint、单元测试、build；布局改动检查实际渲染 |
| `npm/` | 启动器、平台清单 | 平台与版本一致性检查，相关发行测试 |
| 文档与素材 | README、指南、Logo、截图 | 链接和资源路径、示例与实现是否一致 |

Python 与公共检查：

```bash
uv run ruff check .
uv run python scripts/check_dependency_direction.py
bash scripts/check-version.sh
uv run pytest -ra
```

Web 检查：

```bash
npm --prefix web run lint
npm --prefix web test
npm --prefix web run build
```

Rust 和 npm 平台包检查：

```bash
cargo test --locked --manifest-path client/Cargo.toml -j 1
node scripts/check-npm-platforms.js
uv run pytest tests/test_npm_platform_packages.py -o addopts= -q
```

只改文档时不必运行整套业务测试；有命令示例时核对帮助信息与参数。机器资源有限时串行运行检查，把性能测量与其他工作分开。检索与排序变更按 [Benchmark 方案](docs/handbook/benchmark/README.md) 做相应回归，报告里区分实测与未测项。

## 模块、契约与文档约定

- 依赖方向保持 `service → core`，core 不依赖 service、client 或 Web。
- 接口和数据结构变更遵循 [契约变更流程](docs/contracts/PROCESS.md)，同步实现、测试和消费方。
- 操作指南放 `docs/handbook/`；架构入门放 `docs/architecture/`；详细决策放 `docs/design/`。
- 现行图片放 `docs/assets/`；公共评测用例和脱敏报告放 `benches/`。
- 新文档加入对应索引；移动旧文档时保留跳转入口。
- 版本、设备和模型相关结论标明条件，不把历史成绩写成当前测量结果。

## 文件与隐私

凭据放在仓库外的 `$HOME/.key/zace/`。`.env`、运行数据、缓存、日志和临时结果留在被忽略的位置，提交前检查具体 diff。

公开内容不包含 API Key、密码、真实会话、内部源码或未经允许的个人信息。项目首页可以注明由维护者明确公开的 UI 演示地址；其他机器地址不作为通用示例写入文档。外部基准仓库遵守各自许可证。

## 贡献许可

本项目采用 [Unlicense](LICENSE)。提交贡献即表示你有权提交这些内容，并同意按项目协议公开使用。引入第三方代码、图片或资料时注明来源，保留它们要求的许可声明。
