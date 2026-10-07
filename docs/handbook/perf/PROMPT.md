# AI 执行指引：新 VPS 复测

按顺序执行。安装清单见 [ENVIRONMENT.txt](../../../ENVIRONMENT.txt)，
基线与指标见 [README.md](README.md)。用户已授权的恢复、测量和本地文档整理可以直接执行；
不将缺少第三方凭据当成可通过生成随机字符串解决的问题。

## P0：环境恢复

```text
检查 CPU、内存、swap、磁盘、Python/uv/Node/Rust 与 Git 状态。
先审查失效软链接，再备份和重建环境，不直接覆盖密钥或其他开发者的改动。
运行 scripts/setup-dev.sh；密钥放 $HOME/.key/zace/secrets.env，
环境与缓存留在仓库内的忽略目录，依据 ENVIRONMENT.txt 安装。
只报告凭据是否存在和权限，不输出任何内容、前缀或长度。
核对 LangChain checkout 是 e75dae1f53c99c2b5ddb0c7bb36022c6aea25569 且工作树干净。
```

## P1：本地与硬件测量

```text
先停止自己启动的安装、构建和其他测试任务；不要擅自停止已有业务服务。
用 scripts/benchmark-vps.sh local <langchain路径> 串行运行三次，每次独立索引，
保持 500 条/批、并发 4、消费者 2、1024 维。
确认每次 2986 文件 / 20931 chunks、API 请求数为零、无索引错误，
报告 wall_s 中位数、cpu_total_s、peak_rss_mb、阶段耗时及机器负载。
再运行 scripts/benchmark-vps.sh hardware，记录单/双线程 CPU 与单/四连接下载、上传吞吐。
所有产物留在 .local/bench/；硬件探针约 540 MB 流量，不上传源码。
不能仅凭型号或宣称带宽预设性能翻倍。
```

## P2：真实 API 测量

```text
只有在供应商凭据可用时运行 scripts/benchmark-vps.sh ttfb 和 full。
两者发送公共 LangChain 文本并消耗配额；不可称为免费测量。
保持基线配置，报告网络在飞并集、响应字节数、API token、状态码、墙钟、CPU 与 RSS。
出现 429、OOM 或索引错误时停止并报告，不放大内存限制或并发来刷分。
凭据缺失时完成其他独立工作，并把真实 API 测试明确列为未完成。
```

## P3：交付

```text
在 benches/results/ 写设备绑定、脱敏的报告和可核对摘要，
更新 configs/profiles/ 与 HANDOFF.md 的当前状态。
45.462s / 63.326s 对应 e75dae1f、2986 文件、20931 chunks，
不能套到早期 41d3572、2950 文件、20673 chunks。
保留既有改动，检查忽略规则、许可和包内发布文件，执行相关验证及独立只读 review。
未完成 API 测试、未审查的历史资产等限制如实报告；不自动推送或部署。
```
