# UI 演示部署

此目录为独立预览服务：静态页面沿用 `web/dist`，接口由 Node.js 返回合成数据。
不启动 nova-service，不读取实际仓库、账户数据库或供应商密钥。
只有登录和登出有效；保存、删除、注册和模型调用会返回演示模式提示。

部署位置：

- 公网入口：Nginx `:8088`，配置 `/etc/nginx/conf.d/nova-ui-demo.conf`
- 静态文件：`/var/www/nova-ui-demo/`
- 演示 API：`/opt/nova-ui-demo/{server,data}.mjs`，仅监听 `127.0.0.1:8789`
- 常驻服务：`nova-ui-demo.service`，systemd 自动启动与故障重启
- 登录凭据：`/etc/nova-ui-demo/auth.json`，仅 root 可读，由 `LoadCredential` 传给服务

凭据格式为 `{ "name": "...", "salt": "...", "hash": "..." }`。
`hash` 为 Node `crypto.scrypt(password, salt, 64)` 的十六进制结果，默认参数；
密码明文不写入代码或静态产物。会话只存内存，12 小时过期，服务重启后需重新登录。

更新界面：在 `web/` 运行 `npm run build`，将 `dist/` 中除 `*.map` 之外的文件
复制到静态目录。更新演示 API 后运行 `systemctl restart nova-ui-demo`。
修改 Nginx 后先运行 `nginx -t`，通过后再 `systemctl reload nginx`。

检查：`systemctl status nova-ui-demo`、`curl http://127.0.0.1:8088/healthz`。
停止展示：停止 `nova-ui-demo` 服务，并移除本演示站的 Nginx 配置后重载 Nginx。

所有指标、项目、历史与后台主机信息均为模拟数据，仅用于查看排版和交互。
