# 安全问题报告

如果仓库的 GitHub Security 页面提供 “Report a vulnerability”，请使用该私密入口。
若该入口未启用，请通过维护者已有的私密联系渠道先说明问题类型并索取接收方式。
不要在公开 Issue、PR 或讨论中发布可用凭据、账户数据或攻击所需的完整细节。

报告请包含受影响的 commit/版本、部署方式、影响、最小复现和可行的缓解方法。
发送前删除 API Key、Cookie、令牌、SSH 私钥、内部源码和个人信息。
普通功能问题可提交公开 Issue。

## 部署与凭据

- API/LLM 凭据存放在仓库外的 `$HOME/.key/nova/secrets.env`，目录 `0700`、文件 `0600`。
- 生产服务使用 HTTPS；设置 `NOVA_COOKIE_SECURE=true`。
- `nova-service local` 是显式的单用户、无鉴权模式，只绑定回环地址使用。
- 不将索引、账户数据库、请求日志或隐私包提交到仓库。
- 已暴露的凭据应在签发方撤销并重新签发；删除当前文件不能撤销历史中的凭据。

安装与隔离细节见 [ENVIRONMENT.txt](ENVIRONMENT.txt) 和
[隐私资产清单](docs/handbook/privacy/资产清单.md)。
