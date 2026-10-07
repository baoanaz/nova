# 参与开发

安装依赖、配置凭据和工具版本见 [ENVIRONMENT.txt](ENVIRONMENT.txt)。
从仓库根运行 `bash scripts/setup-dev.sh --with-web`，再加载本机 `.env`。
Rust 客户端开发另需 Rust 工具链。设计入口是 [docs/design/INDEX.md](docs/design/INDEX.md)，
开发交接见 [HANDOFF.md](HANDOFF.md)。

## 改动范围

- `core/`：解析、切片、存储和检索，禁止依赖 service/web/client。
- `service/`：REST/MCP、账户、同步和索引任务。
- `client/`：Rust stdio MCP 客户端与同步代理。
- `web/`：React 管理界面。
- `docs/handbook/`：可执行的操作指南。
- `benches/results/`：绑定设备、日期、版本和输入规模的脱敏测试证据。

契约变更遵循 [docs/contracts/PROCESS.md](docs/contracts/PROCESS.md)；
不要在整理文档或环境时改动冻结契约。提交聚焦于一个问题，说明行为变化及验证结果。

## 验证

Python 与公共护栏：

```bash
uv run ruff check .
uv run python scripts/check_dependency_direction.py
bash scripts/check-version.sh
uv run pytest -ra
```

Web 改动：`npm --prefix web run lint`、`npm --prefix web test`、`npm --prefix web run build`。
Rust 改动：`cargo test --locked --manifest-path client/Cargo.toml -j 1`。
检索质量改动还需按 [基准手册](docs/handbook/benchmark/README.md) 做相应回归。
资源有限的机器串行执行各组检查，测试任务与性能测量分开。

## 文件与隐私

凭据放在仓库外的 `$HOME/.key/zace/`；运行数据、环境和临时结果放在被忽略的位置。
提交前检查具体 diff，公开报告不包含 API Key、账户信息、真实机器地址或内部源码。
外部基准仓库保留在仓库外，遵守其自身许可证。安全问题按 [SECURITY.md](SECURITY.md) 报告。

项目沿用 `MIT OR Apache-2.0` 双许可，完整正文见 [LICENSE](LICENSE)。
除非明确声明并由维护者接受，提交的贡献使用同一许可。
