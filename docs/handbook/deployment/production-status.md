# Nova 当前生产部署记录

## 上线状态

- 公网入口：`https://acenova.space`，根路径 SPA、`/api/*` 与 `/mcp` 同源后端。
- 后端：`nova-service.service`，开机启动、失败重启，仅监听 `127.0.0.1:8787`。
- TLS / 反向代理：nginx，配置 `/etc/nginx/conf.d/acenova.conf`。
- 生产代码：`/opt/nova`，由 Git 已提交代码导出，不含开发工作区未跟踪实验文件。
- 静态前端：`/var/www/acenova`，生产构建，根路径部署。
- 持久化数据：`/var/lib/nova`，独立于演示数据和开发 benchmark。
- 生产环境文件：`/etc/nova/service.env`，权限 0600；真实值不写入本文。
- Python 依赖暂复用开发仓库 `.venv`；systemd 使用其 Python，`PYTHONPATH` 指向 `/opt/nova/service:/opt/nova/core`。不要删除此虚拟环境；后续更新依赖前需考虑生产影响。
- 管理员已通过本机 bootstrap 初始化，公网切换前已关闭首次初始化入口。密码不记录在文档中。
- 原 `nova-ui-demo` 保留备用，但域名不再转发至演示服务。

## 已验证

- `nginx -t` 通过，nginx 与 nova-service 均 active / enabled。
- 公网 `/healthz` 返回正式服务，无 demo 标记。
- 公网 `/api/meta`：鉴权开启、非 local 模式、无需 bootstrap；Embedding 与回答模型配置齐备。
- 匿名访问 `/api/projects` 返回 401；管理员 HTTPS 登录、项目列表与登出通过。
- 会话 Cookie 带 Secure / HttpOnly。
- 公网前端产物包含 `@baoanaz/nova-client`、`--yes --prefer-online`、`startup_timeout_sec = 60`。
- 接入指南相关前端测试 9 项、后端相关测试 39 项通过，前端生产构建通过。
- 已完成限定 nginx / systemd / 接入配置的只读子代理审查，无阻断上线的高严重性问题。

未执行真实付费模型/Embedding 请求，因此“配置齐备”不等于已验证供应商余额与推理可用性。npm 发布由独立 release 流水线负责，本站部署不代表 npm 发布状态。

## 更新与回滚

更新：导出已提交代码到生产代码目录，按根路径构建前端，更新静态文件，重启 `nova-service`；更新前备份，数据目录保持不变。

本轮原 nginx 与 demo systemd 配置备份在开发仓库 `backups/vps-production/system/`。必要时恢复原 `acenova.conf`，执行 `nginx -t` 后 reload，即可暂时退回演示站；不要删除或覆盖生产数据。演示站不是可用的生产后端，不应长期作为故障替代。

上线后访问接入指南复制 scoped npm 配置，并在 API Key 页面创建 Agent 用 Key。旧演示账户、演示 API Key 不迁移为生产凭据。
