# npm 包学习：从安装命令到 MCP 客户端

[文档中心](../../README.md) · [客户端配置速查](../../../npm/README.md)

本篇面向想理解 `nova-client` 如何打包、启动和调试的读者。只想接入 Agent，可以先看客户端配置速查。

## 1. npm 安装的是什么

`nova-client` 主包是一段 Node.js 启动器。实际扫描仓库、处理 MCP stdio、同步文件和调用远端 API 的程序由 Rust 编写，随对应平台的子包一起安装。

```text
npm install -g nova-client
          │
          ├── 主包：package.json + run.js + README + LICENSE
          │
          └── 当前平台子包：编译好的 nova-client 或 nova-client.exe
                              │
Agent ── MCP stdio ──► Rust 客户端 ── HTTP ──► nova-service
```

使用发布包只需要 Node.js 18+，无需本机安装 Rust。开发 Web UI 使用的 Node.js 22 是另一项环境要求。

安装或首次 `npx` 会从 npm 下载包；**启动器本身不会再从 GitHub 下载二进制**。客户端连接服务和同步代码仍然需要网络，不能把这一设计理解为全流程离线。

## 2. 主包的几个关键字段

事实来源：[npm/package.json](../../../npm/package.json)。

| 字段 | 作用 |
|---|---|
| `bin` | 将 `nova-client` 命令指向 `run.js` |
| `files` | 控制主包携带的文件；npm 还会按自身规则包含 README、LICENSE 等 |
| `engines.node` | Node.js 最低版本 |
| `optionalDependencies` | 声明六个平台子包及与主包匹配的精确版本 |
| `repository` / `bugs` | npm 页面上的源码和问题反馈入口 |
| `license` | 当前源码后续发行包的许可声明；历史发布版以其随包协议为准 |

子包用自己的 `os` / `cpu` 字段选择安装平台：

| 系统 | x64 | arm64 |
|---|---|---|
| Linux | `nova-client-linux-x64` | `nova-client-linux-arm64` |
| macOS | `nova-client-darwin-x64` | `nova-client-darwin-arm64` |
| Windows | `nova-client-windows-x64` | `nova-client-windows-arm64` |

Windows 的包名使用 `windows`，`os` 字段使用 Node 约定的 `win32`。子包只携带二进制，不另设同名 `bin` 命令。

## 3. 启动器怎样找到二进制

阅读 [npm/run.js](../../../npm/run.js) 时，可以按下面顺序追踪：

1. `platformPackage()` 根据 `process.platform` 和 `process.arch` 选择子包。
2. `fromPlatformPackage()` 检查 npm 安装布局并解析二进制路径。
3. 找不到平台包时，`fromDevChannel()` 尝试开发者指定的 `NOVA_CLIENT_BINARY`，再尝试仓库内 `client/target/{release,debug}/` 的产物。
4. `spawn()` 透传参数、环境与 stdio，转发退出码和信号。
5. 全部失败时，打印平台信息和诊断建议并退出。

**平台包的优先级高于开发通道**。如果在全局安装目录运行 `nova-client`，设置 `NOVA_CLIENT_BINARY` 不一定覆盖已安装的平台包；调试时直接运行刚构建的 Rust 二进制最明确。

MCP 的 stdout 用于协议帧，启动器诊断写入 stderr。在调试启动器时，不要把普通日志写到 stdout。

## 4. 安装与验证

全局安装：

```bash
npm install -g nova-client@latest
nova-client --help
```

不全局安装：

```bash
npx --yes --prefer-online nova-client@latest --help
```

`--help` 能正常输出，只证明启动器和本机二进制能执行。确认真实接入，还需要运行中的 nova-service、有效 API Key，以及 Agent 中一次实际工具调用。

固定版本时，把 `latest` 换成需要的版本；例如 `nova-client@0.0.8`。可以只查询元数据，不触发发布：

```bash
npm view nova-client version
npm view nova-client optionalDependencies
npm view nova-client-linux-x64 version
```

请按实际平台选择第三条命令的包名。不要使用跳过 optional dependencies 的安装方式，否则可能出现主包安装成功但缺少二进制。

## 5. 从源码阅读和调试

在仓库根目录，准备好 Rust stable 工具链后：

```bash
cargo build --locked --manifest-path client/Cargo.toml -j 1
./client/target/debug/nova-client --help
node npm/run.js --help
```

第三条命令在没有已安装平台包时，会使用仓库内构建产物，并在 stderr 打印来源。Windows 产物为 `nova-client.exe`。

仅查看主包发布内容，可以做本地 dry run：

```bash
(cd npm && npm pack --dry-run --ignore-scripts)
```

这不会发布包。仓库中的平台子包目录主要保存清单，真正的二进制由发布构建阶段放入，直接打包主包不等于六个平台都已构建完成。

## 6. 六个平台如何发布

平台清单与生成逻辑在 [make-platform-packages.py](../../../scripts/make-platform-packages.py)，流水线在 [release.yml](../../../.github/workflows/release.yml)。

```text
各平台构建 Rust 二进制
  → stage 对应平台包
  → 发布六个子包
  → 验证子包齐全且版本一致
  → 主包发布到 next
  → 实际安装与启动检查
  → 将验证通过的主包提升为 latest
```

学习或改清单时可以先运行只读一致性检查：

```bash
node scripts/check-npm-platforms.js
bash scripts/check-version.sh
```

维护者发布新版本使用统一入口 `scripts/release-client.sh`，具体参数、鉴权和操作边界见 [npm 发布手册](../release/npm.md)。发布会写入外部 registry 和 Git 标签，不属于本篇的学习验证步骤。

## 7. 常见问题

| 现象 | 优先检查 |
|---|---|
| 找不到当前平台的二进制 | optional dependency 是否安装；registry 是否存在对应子包与版本 |
| Agent 报 stdio 连接关闭 | 先运行 `--help`，查看 stderr 的平台诊断；检查可执行权限 |
| 设置开发二进制却仍运行旧版本 | 平台包优先级更高，直接执行 `client/target/debug/nova-client` 对比 |
| 安装成功但请求返回 401 | 运行中的服务是否需要鉴权，Token 是否属于该服务且未撤销 |
| 手动运行客户端后一直等待 | stdio 客户端在等待 Agent 的协议请求；使用 `--help` 检查程序，用 Agent 验证工具调用 |
| 首次工具调用耗时较长 | 可能正在同步和建立索引，查看服务端项目与索引历史 |

接下来可以阅读 [架构介绍](../../architecture/README.md)，了解客户端之后的索引与检索流程。
