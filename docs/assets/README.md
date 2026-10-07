# 文档资源

[文档中心](../README.md) · [项目首页](../../README.md)

此目录保存 README 和现行使用文档需要的展示素材。

| 路径 | 用途 | 来源 |
|---|---|---|
| [nova-logo.webp](nova-logo.webp) | NOVA 人像与星球标志，登录页和侧栏使用同源素材 | 用户提供的橙金色 `LOGO/LOGO3.png`；使用授权由素材权利人保留 |
| [screenshots/nova-login.png](screenshots/nova-login.png) | NOVA 登录页 | 当前构建，空表单，不含凭据 |
| [screenshots/nova-dashboard.png](screenshots/nova-dashboard.png) | NOVA 紧凑控制台首页 | 当前构建，合成账户及指标；1280×720、1366×768 一屏验证通过 |
| [screenshots/nova-connect.png](screenshots/nova-connect.png) | 接入指南，圆角卡片与方角控件 | 当前构建，API Key 保持空白 |
| [screenshots/nova-login-mobile.png](screenshots/nova-login-mobile.png) | 手机登录页 | 当前构建，390px 宽度 |
| [screenshots/nova-dashboard-mobile.png](screenshots/nova-dashboard-mobile.png) | 手机控制台 | 当前构建，合成账户及指标 |
| [screenshots/connect-nova.png](screenshots/connect-nova.png) | README 快速开始横向截图，NOVA 字标版本 | 用户提供的最新接入指南页面截图 |
| [screenshots/connect.png](screenshots/connect.png) | 接入指南截图兼容路径 | 用户提供的接入指南页面截图 |
| [screenshots/dashboard.png](screenshots/dashboard.png) | 控制台页面截图 | 当前 UI 演示构建，内容为合成数据 |

## 放置约定

- 用户提供的写实 Logo 使用 WebP，浏览器图标使用 PNG；截图使用 PNG，文件名描述页面用途。
- 展示图放 `screenshots/`，Markdown 使用相对链接引用。
- 截图只使用演示数据，不包含真实 Key、密码、个人仓库代码或会话信息。
- 更新截图时替换对应页面文件并检查引用；历史验收图片继续随 `docs/archive/` 中的原记录保存。
- 新增外部素材时登记来源与许可证，不默认把第三方图片纳入项目协议。

UI 的显示名称仍由 [web/site.config.ts](../../web/site.config.ts) 管理；README 的项目文字与本目录 Logo 属于文档资源，改名时需要同步更新。
