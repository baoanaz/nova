# Cloudflare Qwen3 Embedding 有界评测

## 范围与结论

模型 `@cf/qwen/qwen3-embedding-0.6b` 可调用，返回 1024 维向量。官方原生接口、OpenAI-compatible 接口以及 Nova 现有 `create_provider` 的文档/查询调用均实测成功。生产仍使用 Voyage，本轮未切换模型或重建生产索引。

## 官方额度（本轮查证）

- 模型页：<https://developers.cloudflare.com/workers-ai/models/qwen3-embedding-0.6b/>
- 定价：<https://developers.cloudflare.com/workers-ai/platform/pricing/>
- 限流：<https://developers.cloudflare.com/workers-ai/platform/limits/>
- 免费额度：Workers AI 账户每日共享 10,000 Neurons，UTC 00:00 重置（北京时间 08:00）。不是每个模型各送一份。
- 该模型：1075 Neurons / 百万输入 token。若额度只用于它，约合 930 万输入 token/日；其他模型使用会减少余量。模型页单价 $0.0118 / 百万 token，价格表四舍五入为 $0.012。
- Free 用完额度后请求失败；Paid 超额收费。不能称为无限免费。
- 文本 Embedding 默认限流 3000 requests/minute；未发现该模型明确公布的 TPM 上限。RPM 不等于 TPM。
- Cloudflare 托管版本上下文 8192 token，不能直接套用其他部署的上下文长度。

## 实测

固定的 16 个合成 Python 代码片段（含同领域相近干扰项），32 道中英文查询；官方 `documents` 和 `queries` 参数，查询使用代码检索 instruction；余弦排序。

- Top-1：31/32 = 96.875%。
- Recall@3：32/32 = 100%。
- MRR：0.984375。
- 唯一 Top-1 失误：中文“多个进程怎样避免同时修改同一索引？”把 upsert 排在文件锁之前，正确项第 2。

该数据集由本次评测人工构造，候选池很小，不是独立真实仓库基准，也没有与 Voyage 对照。不能据此断言生产准确率或优于 Voyage。查询 instruction 的效果不能等同于 Nova 未适配的默认查询配置。

每档 8 次请求，每次 8 条合成文本，以响应实际 prompt_tokens 统计，无自动重试：

| 并发 | 输入 token | 墙钟时间 | 实测折算 TPM | 请求延迟中位数 |
|---|---:|---:|---:|---:|
| 1 | 21,157 | 11.313 s | 112,207 | 1.221 s |
| 4 | 21,157 | 7.061 s | 179,770 | 2.700 s |

这是短窗口、特定批大小的端到端吞吐，包含网络与响应传输，不是持续一分钟压测，不是平台最大 TPM 或账户配额。全部请求成功，未触发 429，但不能由此推断限流上限。

评测脚本 19 次请求中，原生接口报告的合计用量为 43,839 prompt tokens / 47.109 Neurons；OpenAI-compatible 探针没有返回 usage，未计入该合计。另有一次两文本连通性探针（21 tokens / 0.02257 Neurons）和两次 Nova provider 极小请求；因此上述数字不是账户完整账单。

## 复现与凭据

真实凭据仅存 VPS `/root/.key/nova/cloudflare.env`，目录 0700 / 文件 0600，不提交、不要回显。变量名和查找指引见 `.env.example`、`ENVIRONMENT.txt`。

在仓库根目录运行（会消费账户额度）：

```bash
set -a
source "$HOME/.key/nova/cloudflare.env"
set +a
.venv/bin/python benches/cloudflare_qwen_probe.py
```

脚本只使用合成文本，不上传仓库源码。脱敏结果输出 `/tmp/nova-cf-result.json`；本次快照为同目录 `probe.json`。

## Nova 接入建议

现有 OpenAI-compatible 通道可复用：model 使用上述模型 ID，base URL 指向 `https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/v1`，显式维度 1024、输入上限 8192，先以保守批大小/并发测试。

正式替换前仍需：固定 query instruction 与文档侧策略、验证真实代码检索集、明确免费耗尽后的行为，并重建索引。即使都是 1024 维，Qwen 与 Voyage 的向量空间也不兼容，不能混用。密钥在聊天中出现过，建议评测后轮换并更新私有文件。
