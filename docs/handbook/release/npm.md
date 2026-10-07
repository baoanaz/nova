# nova-client npm 发布

## 1. 架构

`nova-client` 是启动器（`run.js`），真正的二进制由 **6 个平台子包**提供，
它们是主包的 `optionalDependencies`：

```text
nova-client
├── nova-client-linux-x64
├── nova-client-linux-arm64
├── nova-client-darwin-x64
├── nova-client-darwin-arm64
├── nova-client-windows-x64
└── nova-client-windows-arm64
```

npm 按子包自己的 `os`/`cpu` 字段只装本平台那一个，启动器按路径直接执行它。
**没有下载步骤，没有 GitHub Release 二进制资产，没有 fallback。**

## 2. 用户如何使用

```bash
npx --yes --prefer-online nova-client@latest --base-url <服务地址> --token <API Key>
```

- `--yes`：MCP 子进程没有 TTY，缺它可能卡在确认提示；
- `--prefer-online`：跳过本地 npx 缓存，总是解出 registry 上的新版本；
- `@latest`：`next` 是发布中间态，用户不该看到。

## 3. 开发者如何发布

```bash
bash scripts/release-client.sh 0.0.8
```

脚本只做本地准备并 push tag：

```text
检查 main + 工作区干净 + tag 不存在
  → 改版本号（client/Cargo.toml、npm/package.json、server.json）
  → 同步 6 个平台子包 package.json + optionalDependencies
  → check-version.sh 一致性检查
  → commit → push main → push tag v0.0.8
```

**push tag 后就结束，不等 CI。** 结果直接在 GitHub Actions 页面看。

> **Agent 行为约定（硬性）**：执行到 `push tag` 成功即**停止**，只回报
> “vX.Y.Z 已推送 / release CI 已触发”。禁止 `sleep` 等待、禁止轮询
> `npm view`、禁止用 `gh` 或 GitHub API 查 CI 状态、禁止手动重跑或补发。
> 主人几分钟后自己打开 GitHub Actions 页面看即可。
>
> CI 失败时也只做只读分析并汇报；禁止移动 tag、force push、`npm unpublish`、
> 自行发布新版本号或自行改 `latest`。

## 4. CI 做什么

`v*` tag 触发 `.github/workflows/release.yml`：

```text
Build 6 平台
  → 发布 6 个平台子包
  → verify（子包都在 registry 上）
  → 发布主包 --tag next
  → smoke（真实 npx 安装 + 启动）
  → promote：latest → 该版本
  → final verify
```

主包先发 `next` 而不是直接发 `latest`：npm 发布不可逆，latest 是最后一个可控点，
smoke 通过后才切过去。

## 5. 必要约束

- **npm 是唯一二进制分发渠道**。禁止 GitHub Release 二进制资产、
  禁止 runtime 下载 fallback、禁止两套机制并存；
- **`NPM_TOKEN` 只来自 `${{ secrets.NPM_TOKEN }}`**，只在 CI 用；本地脚本不读取任何凭据；
- Windows 子包名用 `windows-*`，而 `os` 字段必须是 `win32`（Node `process.platform` 的取值）；
- macOS x64 / arm64 **各自独立构建**，不做 universal/lipo；
- Linux 用 musl 静态二进制；
- 已发布的版本不可覆盖，**失败就升 patch**，不移动已有 tag。

## 6. 最小排障

| 现象 | 处置 |
|---|---|
| 用户 `npx` 行为没变 | npx 缓存了旧包，确认配置里有 `--prefer-online` |
| 用户报「找不到本平台的二进制」 | 该平台子包没发或版本不一致：`npm view nova-client-<os>-<arch> version` |
| CI smoke 失败 | `npx --yes --prefer-online nova-client@next` 手动复现；查该平台子包能否安装 |
| CI verify 报「查不到」（publish 是绿的） | registry 读端缓存还没传播，稍等几分钟重跑该 job；**不要放弃版本号** |
| `npm publish` 报 `cannot publish over` | 该版本已发布过（不可逆）。换 patch 版本号 |
| CI publish 401 | 缺 `NPM_TOKEN` secret，或 token 不是 Automation 类型 |

发布入口脚本本身的问题用 `bash -n scripts/release-client.sh` 与 dry-run 排查，不要为了验证发布新版本。
