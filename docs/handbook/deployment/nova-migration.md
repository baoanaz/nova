# NOVA 更名与迁移

本次将当前源码、Python/Rust/npm 包、环境变量和界面从 `zace` 统一更名为 `nova`，显示品牌为 **NOVA**。本机源码目录现在为 `/root/xuwenzheng/ace/nova`。

## 接入变化

| 项目 | 新名称 |
|---|---|
| Python 包与命令 | `nova-core`、`nova-service` |
| Python import | `nova_core`、`nova_service` |
| Rust / npm 客户端 | `nova-client` |
| npm 平台子包 | `nova-client-{platform}-{arch}` |
| 环境变量 | 原 `ZACE_*` 改为 `NOVA_*`；`VITE_ZACE_API_BASE` 改为 `VITE_NOVA_API_BASE` |
| 忽略与白名单文件 | `.novaignore`、`.novainclude` |
| 默认数据目录 | `~/.nova` |
| 新 API Key 前缀 | `nova_` |
| 会话 Cookie | `nova_session`（演示服务为 `nova_ui_demo`） |

## 升级现有部署

1. 备份配置与数据，停止需要升级的旧服务。同步服务与客户端代码，避免混用旧命令或旧环境变量。
2. 将部署环境中的 `ZACE_*` 变量改为 `NOVA_*`。若继续使用原数据目录，显式设置 `NOVA_DATA_ROOT` 指向该目录；不要仅依赖新的默认 `~/.nova`，否则会表现为全新部署。数据目录中的账户和 token 哈希无需改写。
3. 将被索引项目内的 `.zaceignore`、`.zaceinclude` 改名为 `.novaignore`、`.novainclude`，保留文件内容。忽略规则指纹可能变化，首次同步可能触发重新扫描。
4. 在新的源码根目录执行 `uv sync --all-packages --all-extras`，用 `uv run nova-service ...` 启动。若移动了整个目录，重新创建虚拟环境或修正其启动脚本路径；不要继续使用失效的旧路径。
5. Rust 使用 `cargo build --locked --release --manifest-path client/Cargo.toml`，更新 MCP 配置中的命令为 `nova-client`。npm 清单已更名，但新包尚未由本次工作发布；完成发布前使用本地构建的客户端，不依赖 `npx nova-client` 已可下载。
6. 重新构建前端 `npm --prefix web run build`，更新部署静态文件及反向代理/systemd 路径。源码中的演示部署模板使用 `nova-ui-demo`；既有机器服务需在实际升级时同步安装这些模板。
7. Cookie 名变化后需要重新登录。现有 API Key 的认证仍按完整 token 哈希查找，原 Key 无需替换字符串；新签发的 Key 使用 `nova_` 前缀。

本机 `.env` 的变量名及项目绝对路径已更新，`NOVA_DATA_ROOT` 仍指向随项目一起移动的 `.local/data`。外置凭据文件继续沿用已有路径，本次未读取或修改其内容。若外置文件还定义旧的 `ZACE_*` 设置，部署前需同步更新这些变量名。

## 交付边界

- GitHub 仓库已正式更名为 [baoanaz/nova](https://github.com/baoanaz/nova)，本地 origin 及当前文档、npm 元数据中的仓库链接已同步。新 npm 包仍需正式发布。
- 在线演示已更新为 NOVA，访问 `http://23.159.248.240:8088/login`。静态文件、API 与 systemd 服务使用 `nova-ui-demo`；原演示账户凭据已原样迁移。公网登录页、健康检查和浏览器 LOGO 加载已验证通过。
- 备份、历史任务、归档及已记录的 benchmark 结果保留历史名称，避免改写原始记录。
- [登录页](../../assets/screenshots/nova-login.png)与[控制台首页](../../assets/screenshots/nova-dashboard.png)来自实际前端构建，接口使用本项目演示数据，不含真实账户、邮箱、密码或 Key。
- 当前 UI 已恢复暖白与珊瑚橙配色，LOGO 换为用户提供的 `LOGO/LOGO3.png`；登录页、侧栏、浏览器图标与文档素材同步更新，原始文件未改动。

## 橙色主题更新验证

按用户反馈恢复暖白与珊瑚橙，改用橙金色 LOGO3。前端 build、lint、113 项单元测试通过，3 项真实服务 e2e 保持跳过；npm 平台配置的 17 项测试通过。桌面 1440px 与手机 390px 的登录页、控制台截图已更新，无横向溢出或页面脚本错误。公网 `8088` 已验证实际背景为 `#FFF7F2`、主按钮为 `#B95336`，LOGO3 和新版浏览器图标正常加载。

橙色版本对比度：主按钮白字 4.82:1，浅橙选中背景上的深橙文字 5.12:1，次级文字/页面 5.31:1，控件轮廓/页面 3.71:1。

## 首次更名验证记录（深色版本）

- 前端：生产 build、ESLint 通过；113 项单元测试通过，3 项需真实服务的 e2e 按原约定跳过。最后修正深色开关滑块的可见性后，build、lint 和共享组件的 9 项测试再次通过。
- Python：全套首跑 1353 通过、2 失败、9 跳过。白名单测试因 `.novainclude` 改名后的字典序变化而失败，Python/Rust 期望已同步修正；另一个耗时测试在并发负载下波动。两项所在模块串行复跑 48 项全部通过，未再次执行整套测试。
- Rust：64 项测试全部通过。Python lint、依赖方向、包版本/平台一致性、设计文档校验和均通过。
- 浏览器：1440px 桌面和 390px 手机宽度均无横向溢出、无页面脚本错误；验证登录、登出、切换统计时间、导航抽屉及注册表单切换。接口由 `web/demo/data.mjs` 提供合成数据；这不替代真实后端 e2e。
- 对比度：正文/卡片 14.21:1，次级文字/卡片 7.35:1，主按钮文字 10.66:1，控件轮廓/卡片 4.18:1。这里验证的是使用中的主题色组合，不宣称完成全站 WCAG 审计。
- 独立只读子代理审查未发现更名或主题对比度问题。审查指出首页两处既有逻辑问题：切换 `days` 时旧请求可能覆盖新响应；`getMe()` 失败时未知配额回退成“不限”。本次保留原业务逻辑，未将品牌任务扩大为数据加载重构。
