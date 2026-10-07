# 文档资源

[文档中心](../README.md) · [项目首页](../../README.md)

此目录保存 README 和现行使用文档需要的展示素材。

| 路径 | 用途 | 来源 |
|---|---|---|
| [logo.svg](logo.svg) | README 项目标识，暖白与珊瑚橙配色、代码括号和偏移阴影 | 本项目绘制，采用项目 Unlicense |
| [screenshots/connect.png](screenshots/connect.png) | README 快速开始横向截图 | 用户提供的接入指南页面截图 |
| [screenshots/dashboard.png](screenshots/dashboard.png) | 控制台页面截图 | 当前 UI 演示构建，内容为合成数据 |

## 放置约定

- Logo 使用 SVG，保持可缩放；截图使用 PNG，文件名描述页面用途。
- 展示图放 `screenshots/`，Markdown 使用相对链接引用。
- 截图只使用演示数据，不包含真实 Key、密码、个人仓库代码或会话信息。
- 更新截图时替换对应页面文件并检查引用；历史验收图片继续随 `docs/archive/` 中的原记录保存。
- 新增外部素材时登记来源与许可证，不默认把第三方图片纳入项目协议。

UI 的显示名称仍由 [web/site.config.ts](../../web/site.config.ts) 管理；README 的项目文字与本目录 Logo 属于文档资源，改名时需要同步更新。
