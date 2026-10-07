# nova 开发交接

> 当前环境更新：2026-10-07 UTC。公开使用入口是 [README.md](README.md)，
> 安装清单是 [ENVIRONMENT.txt](ENVIRONMENT.txt)，操作指南在 [docs/handbook/](docs/handbook/README.md)。
> 本文件记录开发现状；旧机器的环境、服务状态和历史成绩不代表当前 VPS。

## 当前工作区

本轮恢复 Python 3.12.3/uv 0.12.23、Node 22.23.3/npm 10.9.9、
Rust/Cargo 1.99.0 与 C/C++ 工具链。虚拟环境、缓存、索引和测试产物都留在仓库内的忽略目录。
旧失效虚拟环境与改动前重要文件备份于本机 `backups/`。

凭据统一在 `$HOME/.key/nova/secrets.env`，目录 0700、文件 0600；
仓库内 `.env` 只加载外部密钥。Voyage 与 LLM 凭据已恢复并通过实际请求验证；
LLM 使用用户指定的 DeepSeek 模型，网关地址与认证均不进入公共配置。
已有 SSH 部署密钥与供应商 API Key 不是同一类凭据。

已识别的 5 份内部用例/问答/报告及私有清单已迁至 `$HOME/.key/nova/benchmarks/`；
公开默认清单只保留公共靶场与自检，内部测试通过 `--targets-file` 显式选择。
Git 历史仍含迁移前内容，公开历史前需要另外处理。

本机 `nova-service` 未启动，旧持久基准索引没有出现在历史文档指定位置。
不假定新机器已经部署或已经有可复用索引。环境恢复不会自动启动生产服务。

## 新 VPS 复测

设备为 EPYC 7282、2 vCPU、1919 MiB RAM、1535 MiB swap。
LangChain 保持 `e75dae1f53c99c2b5ddb0c7bb36022c6aea25569`，
每轮 2986 文件 / 20931 chunks。并发 4、消费者 2、1024 维。

- 本地冷启动三轮 44.543 / 39.775 / 40.950s，中位数 40.950s，比旧机 45.462s 缩短 9.92%。
- CPU 累计中位数 36.656s，比旧机 44.965s 缩短 18.48%；未兑现早期“翻倍”的预测。
- OVH 直连单连接/四连接下载为 74.818/199.331 Mbps；Cloudflare 上传为 297.422 Mbps。
- Cloudflare 下载曾返回 403，已保留失败记录，未用错误响应计算吞吐。
- 真实 API full 单次 52.664s，比旧机 63.326s 缩短 16.84%；58 请求全部 HTTP 200，零索引错误。
- TTFB 并发 1/4/8，每档 8 请求均 HTTP 200；聚合响应速率 6.49/19.30/29.10 MiB/s，不能当作端口带宽。

证据与限制见 [设备报告](benches/results/index-perf-epyc-2c2g.md)。
入口为 `scripts/benchmark-vps.sh`，默认结果在 `.local/bench/`。
性能靶场版本与早期 golden 的 `41d3572` 不同，不混用文件数和耗时。

## 架构与工具语义

依赖方向为 `web, client → service → core`，由 CI 强制。

| 路径 | 职责 |
|---|---|
| `core/` | tree-sitter 解析、切片、SQLite/FTS、LanceDB、检索和 ContextPack |
| `service/` | REST/MCP、鉴权/租户、同步/索引、审计/配额、LLM 总结 |
| `client/` | Rust stdio MCP、扫描/忽略/哈希/增量上传 |
| `web/` | React/Vite 管理界面 |
| `npm/` | 客户端启动器和六个平台包清单 |

`search_context` 返回 ContextPack Markdown，不调用 LLM。
`ask_project` 先判断证据：不可回答时返回 insufficient_evidence；
总结模型未配置或调用失败时 degraded；成功时 answered，并回验引用。
`reason`、证据编号、Missing Evidence 和 Suggested Next Queries 都是重要返回语义。

## 产品开发与约束

任务板为 [docs/tasks/README.md](docs/tasks/README.md)；
新工作优先参考 [optimization-backlog.md](docs/plan/optimization-backlog.md)。
已登记的后续工作包括 TASK-093 真实数据采集、TASK-023 真实场景用例。

- 检索质量参数 R29/R30 仍冻结；不根据现有 smoke/golden 集拟合后直接调参。
- `docs/contracts/**`、`docs/design/**` 和 `core/nova_core/{types,interfaces,hashing}.py`
  属冻结面，变更遵循 [契约流程](docs/contracts/PROCESS.md)。
- TASK-109 已合并；不要把历史 pending 标记当作未完成。
- TASK-114/115 已优化 CLI 整仓冷启动，但实际客户端仍有串行分批上传和请求内 ingest，
  CLI 收益未必直接等于生产接入收益，见 backlog S1/S2。
- 多消费者要攒满整窗派发，不能按 K 切小窗口来维持并发。

## 已知缺口

下列问题来自既有审查，本轮环境与文档整理未修改业务代码：

- `/healthz` 的匿名项目进度信息需进一步收敛（backlog A2）。
- 符号级定位、负例可回答阈值、调用链命中仍需真实数据驱动。
- API 的 output_dimension 透传、chunk 参数配置与 full_reparse 成本仍待处理。
- 共享仓库认领、多租户组织模型、端到端测试覆盖仍有后续工作。
- 历史记录中的日志归属、契约 YAML 语法等缺口按对应任务卡核对，不在环境整理中顺手修改。
- 历史截图、旧证据及 Git 历史未做全量脱敏审计，不能宣称整个历史没有敏感内容。

## 验证与发布

验证命令见 [CONTRIBUTING.md](CONTRIBUTING.md)。
2 GiB VPS 串行运行 Python、Web、Rust 各组检查；core 按子目录、service 按测试文件拆进程释放内存。
整组长进程曾触发 MemoryHigh 回收限速，已终止后拆分重跑，不能把中断记为通过。
计时测试与构建、安装、常规测试分开执行。

本轮结果：Python 1362 通过、9 跳过（core 771、service 552、root 17、benches 22）；
Web 109 通过、3 跳过，lint/build 通过；Rust 61 项通过。
Ruff、依赖方向、版本一致性、隐私包往返恢复、改动文档链接检查均通过。
独立只读复审无阻塞问题；验证日志保存在本机 `.local/logs/checks/`。

仓库采用与既有 npm/Rust 声明一致的 MIT OR Apache-2.0。
当前变更包括原有未提交的性能手册；不要覆盖其他开发者改动。
提交、推送、发布 tag 和生产部署是不同动作，不把旧交接文档中的授权当成当前会话授权。
