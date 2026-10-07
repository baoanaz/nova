# zace-client

zace 的 MCP stdio 客户端：编辑器把它作为子进程拉起，它负责在本地扫描/增量上传代码，
把检索交给远端 `zace-service`（切片、索引、检索、渲染都在服务端）。

支持 **Claude Code / Codex / pi（经 pi-mcp-adapter）/ Cursor** 等所有支持 stdio 的 MCP 客户端。

本页是安装与接入速查。想了解启动器、平台包和 Rust 二进制的关系，见
[npm 包学习](https://github.com/baoanaz/zace/blob/main/docs/handbook/getting-started/npm-client.md)；
需要先运行后端与 UI，见 [本地部署指南](https://github.com/baoanaz/zace/blob/main/docs/handbook/deployment/local.md)。

## 快速开始

全局安装（装完 `zace-client` 进 `PATH`）：

```bash
npm install -g zace-client@latest
```

也可以不全局安装，直接用 `npx`（首次运行会从 npm 获取包）：

```bash
npx --yes --prefer-online zace-client@latest --base-url http://127.0.0.1:8787 --token "<你的 API Key>"
```

两种方式都会从已安装的**平台子包**启动 Rust 客户端（stdio），启动器不再从 GitHub 下载二进制。客户端随后连接你配置的 `zace-service`。

## 客户端配置

三个 Agent 用的是同一套 stdio 配置，只是文件位置不同。

### Claude Code

```bash
claude mcp add-json zace --scope user '{
  "type": "stdio",
  "command": "npx",
  "args": ["--yes", "--prefer-online", "zace-client@latest", "--base-url", "http://127.0.0.1:8787", "--token", "<你的 API Key>"]
}'
```

### Codex CLI

`~/.codex/config.toml`：

```toml
[mcp_servers.zace]
command = "npx"
args = ["--yes", "--prefer-online", "zace-client@latest", "--base-url", "http://127.0.0.1:8787", "--token", "<你的 API Key>"]
startup_timeout_ms = 60000
```

### pi（通过 pi-mcp-adapter）

pi 本身不含 MCP，需先装适配器：`pi install npm:pi-mcp-adapter`。
然后写标准的 `.mcp.json`（项目级）或 `~/.config/mcp/mcp.json`（全局），适配器会自动读取：

```json
{
  "mcpServers": {
    "zace": {
      "command": "npx",
      "args": ["--yes", "--prefer-online", "zace-client@latest", "--base-url", "http://127.0.0.1:8787", "--token", "<你的 API Key>"]
    }
  }
}
```

### 其它（Cursor 等 JSON 配置）

```json
{
  "mcpServers": {
    "zace": {
      "command": "npx",
      "args": ["--yes", "--prefer-online", "zace-client@latest", "--base-url", "http://127.0.0.1:8787", "--token", "<你的 API Key>"]
    }
  }
}
```

## 配置项

| 参数 | 环境变量 | 必填 | 说明 |
|---|---|---|---|
| `--base-url` | `ZACE_BASE_URL` | 是 | `zace-service` 基础地址（须带 `http://` 或 `https://`） |
| `--token` | `ZACE_API_TOKEN` | **普通 `serve` 模式必填** | 在实际服务的「API Key」页创建。只有显式 `zace-service local` 的无鉴权本地模式可以省略 |
| `--cache-root` | `ZACE_CLIENT_CACHE` | 否 | 本地索引缓存根，默认 `~/.cache/zace` |
命令行参数优先于环境变量。

## 工具

| 工具 | 作用 |
|---|---|
| `search_context` | 检索与问题最相关的上下文（代码/调用链/文档证据包，带文件:行号） |
| `ask_project` | 项目级综合问答（带引用的 grounded answer，证据不足时如实说明缺口） |

## 二进制从哪来（npm 平台子包）

`zace-client` 本身只是启动器（`run.js`）；真正的二进制由 **6 个平台子包**提供，
它们是主包的 `optionalDependencies`：

```text
zace-client                    ← 启动器
├── zace-client-linux-x64      ← 含 zace-client 二进制
├── zace-client-linux-arm64
├── zace-client-darwin-x64     ┐ 两个架构各自独立构建
├── zace-client-darwin-arm64   ┘
├── zace-client-windows-x64
└── zace-client-windows-arm64
```

npm 会按子包自己的 `os`/`cpu` 字段**只装本平台那一个**（其余跳过），
启动器直接执行它。**安装阶段从 npm 获取包，启动器不额外下载二进制。**
Rust 客户端的同步与服务请求仍使用网络，客户端扫描缓存与 npm 安装缓存也仍存在。

### 为什么不用 GitHub 下载

早期形态是「启动器按版本号去 GitHub Release 下载二进制」，两个问题：

1. **Node 默认不读 `https_proxy`**（只认 `NODE_USE_ENV_PROXY=1`，v20+），
   代理环境里启动器直连 GitHub → 命中共享出口 IP 的 API 限流：
   同一时刻 `curl`（走代理）200、`node`（直连）403。
   用户看到的是 `MCP server failed to start: connection closed`，排查代价极高。
2. **版本对齐是人为纪律**：`npm publish` 必须等 Release 资产全绿，慢一步就是 404。

而保留下载路径 = **两条分发渠道并存**，出错时无法判断用户拿到的是哪个二进制。
故「npm 是唯一二进制分发渠道」（D-48/D-49）。

### 没有回退链（这是故意的）

子包找不到就**直接退出并打印诊断**，不偷下载、不静默降级。

**唯一的例外是开发期通道**（不是用户分发路径，命中时会打印用了哪一条）：

1. `ZACE_CLIENT_BINARY=/abs/path/zace-client` —— 开发者显式指定；
2. 仓库内 `client/target/{release,debug}/zace-client` —— `cargo build` 后直接跑。

刻意**不含**「PATH 里的 zace-client」：npm 安装的 shim 就叫这个名字，
回退到它会把包装器自己当二进制，造成无限自我递归（TASK-099 实测踩到）。

> 维护者发布只跑一条命令：`bash scripts/release-client.sh x.y.z`
> （详细 SOP 见 [`docs/handbook/release/npm.md`](../docs/handbook/release/npm.md)）。
> 流程是**先发 6 个子包 → 验证 → 发主包（`--tag next`）→ 冒烟 → promote 到 latest**，
> 缺任一平台 = 该平台用户装不上（且是**静默**故障——npm 不报错，
> 只是跳过解析不了的可选依赖）。

## 已知限制

- 普通 `zace-service serve` 默认启用账户鉴权，token 无效或已撤销时返回 401。
  只有显式 `zace-service local --repo ...` 才是无鉴权的本地模式，此时 `--token` 可省略。
- 首次索引大仓库时，第一个 tool call 会在上传期间等待（进度反馈属后续卡）。

## 从源码构建

```bash
git clone https://github.com/baoanaz/zace && cd zace/client
cargo build --release        # 产物：target/release/zace-client
```

`npm/run.js` 找不到平台子包时会回退到**开发期通道**：`ZACE_CLIENT_BINARY` 指定的二进制，
或仓库内 `client/target/{release,debug}/zace-client`。（用户分发路径只有 npm 子包。）

## 许可证

当前源码采用 [Unlicense](https://github.com/baoanaz/zace/blob/main/LICENSE)，后续发布同步该声明。
已发布的历史版本以其包内 LICENSE 与元数据为准。
