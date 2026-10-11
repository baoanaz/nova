# nova-web

[文档中心](../docs/README.md) · [本地部署](../docs/handbook/deployment/local.md) · [UI 演示部署](demo/README.md)

nova 的 React / Vite 管理界面：账户、项目、MCP 接入、API Key、历史记录、模型设置与管理员后台。页面通过 REST API 与 nova-service 通信，代码检索和问答由服务端与 Agent 完成。

## 页面

路由以 [src/app/App.tsx](src/app/App.tsx) 为准：

| 页面 | 路径 | 用途 |
|---|---|---|
| 登录 / 注册 / 初始化 | `/login` | 首个账户初始化、登录和邀请码注册 |
| 控制台 | `/` | 账户、服务模型与调用统计 |
| 项目 | `/projects` | 项目列表与索引信息 |
| 接入指南 | `/connect` | npm 安装、MCP 配置和可选提示词 |
| API Key | `/keys` | 创建、查看与撤销 Key |
| 历史记录 | `/history` | 索引与使用历史 |
| 设置 | `/settings` | 自定义 LLM 模型、接口和 Key |
| 后台 | `/admin` | 管理员功能；前端入口与后端权限双重校验 |

普通 `serve` 模式下，没有账户时进入初始化，已有账户而未登录时进入登录页。显式 `local` 模式直接使用隐式本地账户。判断由 `/api/meta` 与 `/api/auth/me` 共同完成。

## 开发

使用 Node.js 22。在仓库根目录按 [本地部署](../docs/handbook/deployment/local.md) 安装依赖并加载配置。

终端 A：

```bash
uv run nova-service serve --host 127.0.0.1 --port 8787
```

终端 B：

```bash
npm --prefix web run dev -- --host 127.0.0.1 --strictPort
```

打开 <http://127.0.0.1:5173>。Vite 将 `/api`、`/healthz` 代理到 `NOVA_WEB_API`，默认是 `http://127.0.0.1:8787`。

生产构建输出到 `web/dist/`，由反向代理同源托管静态页面与 API。子路径部署可设置 `NOVA_WEB_BASE`，它同时决定 Vite 资源前缀和路由 basename，见 [VPS 部署](../docs/handbook/deployment/vps.md)。

## 品牌与主题

[site.config.ts](site.config.ts) 控制 UI 显示名称，默认 `name: "NOVA"`。修改后重新构建，侧栏、移动端顶栏、登录页和浏览器标题一起更新，`tagline` 控制品牌说明。

显示名称与客户端包名、`nova_` API Key 前缀分别管理。后两者属于实际接入约定，修改 UI 品牌时不自动改变它们。

主题采用 Warm Minimalism：暖白底与侧栏 `#FAF9F5`、白卡片 `#FFFFFF`、次级背景 `#F5F4ED`、暖灰区域 `#F0EEE6`；正文 `#141413`、次要文字 `#5E5D59`、分隔线 `#E8E6DC`。品牌装饰橙 `#D97757` 与主要操作底色 `#292522` 分离，主要按钮 Hover 使用 `#403934`、Active 使用 `#141413`、文字为白色；创建、保存、登录、注册与主要 CTA 使用独立的 `primary` Token，次级按钮保留暖白描边，筛选与开关保留陶土橙强调（`#B64B32`），危险操作保留红色；浅橙选中背景 `#FBE7DA` 上的小字号强调使用更深的 `#A1462D`。辅助灰 `#B0AEA5` 不用于正文。成功保留语义绿色（轮廓 `#788C5D`，文字另用深绿），警告和错误保持独立语义色。控件使用独立的 `ink.control` 保持可辨识边界，输入框包含自动填充底色覆盖。所有颜色与阴影 Token 集中在 `src/index.css` 的 `:root`，`tailwind.config.js` 仅映射变量，支持透明度修饰符。登录页保留原 NOVA 字标与视频背景，Hero 使用浅蜜桃色 Token；不改变字体、布局或业务交互。桌面是左侧导航与右侧内容，窄屏使用导航抽屉。Logo 与脱敏截图见 [docs/assets/](../docs/assets/README.md)。

主卡片保留圆角，采用 1px 暖灰边框与轻微阴影；侧栏选中项、按钮及默认输入框使用方角。共享卡片裁切标题栏背景，避免方形标题栏覆盖外层圆角。控制台保留三张主卡片：账户资料压缩行距、模型横向分组、工具调用在桌面横向排列三组；手机端纵向展开，不隐藏指标或强制截断内容。合成数据下已验证 1280×720、1366×768 桌面无需纵向滚动。

## 检查命令

在 `web/` 目录执行：

| 命令 | 作用 |
|---|---|
| `npm run dev` | 开发服务器与 API 代理 |
| `npm run build` | 类型检查与生产构建 |
| `npm run lint` | ESLint 检查 |
| `npm test` | 单元测试；依赖真实服务的 e2e 默认跳过 |

对接真实服务的测试，要求该服务已有测试账户：

```bash
cd web
NOVA_E2E=1 NOVA_E2E_BASE=http://127.0.0.1:8891 \
NOVA_E2E_USER=测试账户 NOVA_E2E_PASSWORD='填写测试密码' \
VITE_NOVA_API_BASE=http://127.0.0.1:8891 \
  npx vitest run src/pages/console.e2e.test.tsx
```

该测试包含 Node/jsdom 所需的 cookie 转发适配；浏览器实际访问使用同源 cookie。真实服务验证与模拟 UI 演示是两个独立场景。

## 源码目录

```text
web/
├── site.config.ts  UI 显示品牌
├── src/
│   ├── api/        请求出口、类型与错误文案
│   ├── app/        路由、账户门禁、布局、MCP 配置片段
│   ├── components/ 共享组件
│   └── pages/      各页面的请求与内容组合
└── demo/           合成数据 API 与演示站部署文件
```

组件不直接发业务请求，API 错误统一展示；未测量的指标显示 `—`，不以 0 代替；界面不另行实现一套 ContextPack 解析与检索逻辑。接口定义见 [OpenAPI](../docs/contracts/openapi.yaml)，接入片段用法见 [npm/README.md](../npm/README.md)。
