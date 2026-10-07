# 新 VPS 性能复测

本手册测量本机处理速度、直连网络吞吐和真实 API 冷启动。
安装要求见 [ENVIRONMENT.txt](../../../ENVIRONMENT.txt)，
指标定义见 [探针说明](../../../benches/embed-bench/README.md)。
AI 执行指引见 [PROMPT.md](PROMPT.md)。

## 1. 可比基线

TASK-115 的原始证据对应以下输入，不能与早期 golden 靶场版本混用：

| 项目 | 固定值 |
|---|---|
| LangChain commit | `e75dae1f53c99c2b5ddb0c7bb36022c6aea25569` |
| 解析文件 / chunks | 2,986 / 20,931 |
| embedding | API，`voyage-4-lite`，1024 维 |
| 批大小 / 并发 / 消费者 | 500 / 4 / 2 |
| 旧设备 | `vps-la-2c2g`，Xeon E5-2680 v4，2 vCPU，1.9 GiB |

旧设备探针基线：

| 模式 | 墙钟 | 峰值 RSS |
|---|---:|---:|
| local-only，K=2 | 45.462s | 777.2 MiB |
| full，K=2 | 63.326s | 823.4 MiB |
| full，K=1 | 79.49s | 748.4 MiB |

来源：[TASK-115 报告](../../../benches/results/index-perf-task115-vps.md)、
[local JSON](../../../benches/results/raw/task115/langchain-local-k2.json)、
[full JSON](../../../benches/results/raw/task115/langchain-api-k2.json)。
旧机网络报告约 90 Mbps，但不是与新硬件探针相同端点、相同工具的测试，不能作严格倍数对比。
早期 `41d3572` 的 2,950 文件 / 20,673 chunks 不适用于本表。

## 2. 准备

```bash
bash scripts/setup-dev.sh --with-web
set -a; source .env; set +a
```

靶场放在本仓库外。没有靶场时：

```bash
mkdir -p ../benchmark
git clone https://github.com/langchain-ai/langchain.git ../benchmark/langchain
git -C ../benchmark/langchain checkout e75dae1f53c99c2b5ddb0c7bb36022c6aea25569
```

只读使用靶场，不修改源码或提交文件。若已有 checkout 不是此版本，先单独准备正确版本，
不要为了测试擅自切换他人正在使用的工作目录。
先检查磁盘、swap、可用内存与其他进程；2 GiB 机器必须串行运行索引和测试。
脚本的索引内存护栏为 MemoryHigh=1200M、MemoryMax=1500M、MemorySwapMax=512M。
测量应使用磁盘目录；默认结果和索引落在被忽略的 `.local/bench/`。

## 3. 免费本地测量

```bash
bash scripts/benchmark-vps.sh local ../benchmark/langchain
bash scripts/benchmark-vps.sh hardware
```

local-only 保留 API provider 的本地准备与入库逻辑，只替换网络请求，明确设置 API 模式以免加载 ONNX。
此模式强制 Hugging Face 离线，不需要真实 Key。每次运行创建全新索引；
至少重复三次并记录中位数，同时检查 `chunks_new=20931`、`files_parsed=2986`、零请求和空 errors。

hardware 使用 sysbench 的 prime=20000、单线程/双线程各 10 秒；OVH 公共文件测直连下载，Cloudflare 测上传。
默认单连接下载 100 MiB、4 连接各下载 100 MiB、上传 16 MB，合计约 540 MB 流量。
可用探针的 `--download-url` 和 `--download-bytes` 指定另一个公开 HTTPS 文件及其完整长度。
它禁用代理、不发送源码；吞吐包含连接与响应延迟，只代表到该端点的实际路径，
不等同于运营商承诺的端口速率。端点报错或传输不完整时保留错误，不能把错误响应当测速数据。

## 4. 需要凭据的 API 测量

在 `$HOME/.key/zace/secrets.env` 配置供应商签发的 `EMBED_API_KEY` 后：

```bash
bash scripts/benchmark-vps.sh ttfb ../benchmark/langchain
bash scripts/benchmark-vps.sh full ../benchmark/langchain
```

ttfb 扫描并发 1/4/8，每档 8 请求，使用公共 LangChain chunk 文本；
full 单次历史计费约 3.56M token、响应体约 228.6 MiB。
这两项都调用外部 API，消耗配额。缺少 Key 时入口脚本直接退出，不发送请求。
先确认本地测量口径正确，再进行真实 API 测试；出现 429、索引错误或 OOM 时报告并停止。

API 吞吐包含服务端计算、网络、响应解码等因素。TPM/RPM 是账号限额，
`tokens / TPM × 60` 只能提供平均速率预算参考，存在突发额度时不能作为单次测试的硬性耗时下限。
CPU 型号、标称带宽和规格预测都不能代替实际测量。

## 5. 报告与配置

保留原始 JSON 在 `.local/`，发布脱敏摘要和设备报告到 `benches/results/`。
报告应包含 UTC 日期、设备规格、zace commit/工作树状态、靶场 commit、文件和 chunks 数、
模型/维度/批大小/并发/消费者数、墙钟、CPU 累计、RSS 与异常。
本地和网络阶段存在重叠，不直接相加，也不能简单用 full 减 local-only 得到网络耗时。

硬件档案以规格别名命名，例如 `epyc-2c2g.env`；不使用真实 IP 或主机名。
只把非密钥参数放入 [configs/profiles/](../../../configs/profiles/README.md)。
本地测量完成不代表 API 调优完成，未完成项必须明确标注。
