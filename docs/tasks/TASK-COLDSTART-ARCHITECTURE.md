# 冷启动架构：合并同步与离线回归

用户要求核对 `optimization-backlog.md` 的 S1/S2 后开始优化，优先处理整体冷启动耗时，保持实现简单可维护，并避免非 embedding 性能测试调用付费 API。本卡记录此次授权范围和兼容契约追加，不修改旧调用的默认语义。

## CF-05 变更说明与执行边界

- `batch-upload` 和 `deletions` 可选 `deferIndexing: true`：只持久化内容和待处理账本。缺省仍在请求内完成索引。上传响应明确 `indexingDeferred`，延迟模式 `report: null`，accepted 只表示持久化成功。
- 增加 `POST /api/sync/flush {projectId}`：合并持久账本中的待处理文件，复用单项目 `ProjectIndexer` 后台运行，幂等触发；重复请求不堆积任务。
- `sync/status` 追加 `indexProgress`、`skippedFiles`；pendingJobs 包含已上传待索引的工作。客户端使用 `progressOnly=true` 轻量轮询，运行中只读 worker 快照。完成前不得将 accepted 当作就绪。
- 客户端完成上传/删除后 flush 并轮询状态。120 秒内未完成即提示后台继续，下次调用重新检查。缓存记录上传成功与索引完成分离；未完成不发检索。
- 待处理项保存在现有同步 JSON 账本，索引全部成功后才清除；失败/重启保留，显式 flush 重试。仍是单进程服务的每项目锁模型，不引入队列基础设施。
- 新客户端对旧服务兼容：未确认延迟上传的旧服务如不支持 flush，可保留原同步语义。服务确认延迟后缺失 flush 必须报错。

## 基线与计量

使用既有 LangChain `e75dae1f53c99c2b5ddb0c7bb36022c6aea25569` 索引导出真实 float32 向量，按完整输入 SHA-256 回放；缺失直接失败，不存在网络回退。18722 个不同输入、1024 维，87,859,200 字节，放在忽略目录 `.local/fixtures/`。

- 优化前 core：44.990 秒，37.785 CPU 核秒，754.2 MiB RSS。
- 优化前 Rust MCP 客户端→隔离服务→检索：73.083 秒，服务 CPU 54.082 核秒，20 次独立 ingest；其中 graph.resolve_phase2 累计 17.536 秒。
- 两者均 2986 文件 / 20931 chunks，索引 errors 为空。单次样本；本机回环无真实上行延迟。
- 回放不包含 Voyage 推理、下载和 JSON 解码，也不验证检索质量。查询使用录制 passage 向量只为走通用户路径。新 fixture 创建不产生 API 调用。
- systemd 基准限制外网，只允许 localhost；输出 `.local/bench/architecture-before-{core,client}/`。

## 交付状态

已完成兼容同步接口、持久重试、并发/删除/失败恢复保护，全量重建逐文件投递和尾窗/图阶段重叠。最新用户路径 52.710 秒 / 37.990 CPU 核秒 / 628.2 MiB RSS，一次 ingest，文件及向量数量一致、无错误。测试：Rust 64 项、core 52 项；相关服务 78 项通过，最后补充边界保护后 deferred sync 13 项再次通过。

完整数据与计量限制见 [性能报告](../../benches/results/coldstart-architecture-epyc.md)，日常离线入口见 [探针说明](../../benches/embed-bench/README.md)。core 单独回放未证明更快；用户路径峰值 RSS 增加约 20%。120 秒限制从上传完成后开始，尚不是首同步全过程预算，也未实现结构化“索引中”上下文包。

独立只读审查受外部认证、空回答和 502 阻塞，已停止重试，**未宣称审查通过**。未提交、推送或部署。
