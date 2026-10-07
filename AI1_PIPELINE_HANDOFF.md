# AI1 有界扫描—上传—索引流水线交付

基点：`2ce75abf5c8573a6b445143b7bda88f56d1d511e`。
分支：`ai1/bounded-sync-pipeline`。
本次不修改 storage/store.py、storage/db.py、tokenizer、主工作区或冻结 candidate。

## 协议与完成边界

1. resolve 后，`POST /api/sync/flush {projectId, begin:true, sessionId}` 开始输入会话。
   sessionId 使用本次 tool call ID，持久化在 sync-state.json。新会话接管后，旧会话的
   上传、删除、输入封口及 checkpoint 被 409 拒绝；旧协议写入也会使当前会话失效。
2. 扫描在线程池运行，向容量为 2 的 Tokio 通道发送批次；每批最多 64 文件、512 KiB
   sanitized 内容。文件实际读取也受原有 128 KiB 文件上限约束，防止 metadata 后增长。
   主流程逐批上传，不再持有全仓库源码。元数据清单和路径集合仍随文件数增长。
3. 上传携带 `{pipeline:true, deferIndexing:true, sessionId}`。服务器限制流水线请求
   最多 64 文件、解码内容最多 1 MiB。在接收下一批前完成已有 pending 工作，形成背压；
   接收后立即启动原有单项目后台 worker。`accepted` 只代表 blob 和 pending 账本持久化。
4. 扫描结束后，带 `{sessionId, blobHashes:完整清单}` 调用 flush。服务器在同一项目锁内
   验证全部 hash 已存在，删除清单外遗留路径、登记待删索引，然后持久化 inputClosed。
   这也修复“上传响应丢失后，本地删除了那个未入缓存文件”的恢复路径。封口后拒绝新输入。
5. worker 将持久化积压分为每次最多 64 个输入文件 / 1 MiB，以及最多 64 个删除路径。
   一批完整 ingest 返回之后才移除该批 pending；失败批次和后续批次保留，以供重试。
   服务端项目锁仍覆盖索引，防止源版本被覆盖。图、向量和 store 的关闭/落盘路径没有迁出
   ingest，也未改变任何计时器。
6. 输入尚未封口时，status 即使当前没有 pending 文件也报告 pendingJobs=1。
   客户端等待 worker 不在 running、pendingJobs=0，并剔除服务端报告的跳过文件。
7. `checkpoint {sessionId, seal:true, blobHashes:最终可检索清单}` 验证当前会话、输入已封口、
   pending 为空及清单精确匹配，然后持久化 checkpoint，返回 `sealed:true`。
   客户端不再复用旧 checkpoint 作为本次确认，不会在封口失败时降级为无 scope 检索。
   空清单也先完成删除和 checkpoint，随后向用户报告无可检索文件。

blob 通过临时文件 + fsync + rename 原子发布；同步账本与 checkpoint 通过临时文件 +
fsync + replace + 目录 fsync 确认。所有这些操作都在上传/封口响应之前完成。
本改动没有重新定义底层 SQLite/图/向量在断电时的耐久性；沿用 core 的关闭边界。

## 恢复和兼容性

- 网络断线/响应丢失：未确认的客户端缓存不提交，下次重传；服务端内容寻址复用已有 blob。
- 服务重启：从持久化 pending 恢复；无内存任务队列依赖。新调用会接管会话并重新封口。
- 索引失败：已确认上传仍可命中客户端缓存；重新 flush 重试未完成批次。
- 服务端缺失/覆盖了缓存中的 hash：manifest_conflict 会使本地缓存失效，下次重传。
- 当前会话失败不会把另一个会话的数据误当作自己的成功结果。
- 新服务端保留无 session 的旧请求语义；新客户端要求新服务端的会话与 sealed 显式确认。
  部署时先更新服务端，再更新客户端。
- 有界的是活跃内容与解析批次，不是整个项目的元数据、已索引图/向量或历史 blob 磁盘用量。
  最终清单删除的孤立 blob 暂保留内容寻址文件，不新增全目录 GC。
- 项目内写入串行；扫描、网络接收可与前一批索引重叠。批次大小及新增 fsync 的性能影响
  尚未测量，不宣称已达到 30 秒目标。

## 验证

已运行轻量协议测试（受控 ingest 桩，无 embedding 调用）：

```sh
PYTHONPATH=service:core /root/xuwenzheng/ace/nova/.venv/bin/python -m pytest \
  -o addopts='' -q service/tests/test_sync_pipeline.py
```

覆盖容量/背压、上传确认与 readiness 分离、断线重放、新 EngineManager 恢复、失败批次
保留与继续处理、同路径覆盖、过期会话、旧客户端写入冲突、孤立上传对账、空清单删除、
blob 原子发布失败和 checkpoint 磁盘失败。

Python 改动的 Ruff 检查和 `git diff --check` 已通过。Rust 回归代码已补齐：扫描批次上限、
消费方取消、真实 HTTP 断线、客户端重启后重传、封口冲突禁止查询、缺少 sealed 确认拒绝，
以及 call ID 全链路夹具适配。按分工未执行 Rust 编译或测试。

独立只读 reviewer 已调用，但 advisor 接口 30 秒超时，未收到审查结论。

## 主负责人串行执行

请在隔离分支上先运行以下测试，再用固定 LangChain 语料与已有向量 fixture 做完整验收：

```sh
cargo test --manifest-path client/Cargo.toml --lib index::tests
cargo test --manifest-path client/Cargo.toml --test sync_deferred --test call_id
PYTHONPATH=service:core python -m pytest -o addopts='' -q \
  service/tests/test_sync_pipeline.py service/tests/test_sync_deferred.py \
  service/tests/test_sync_api.py
```

完整计时必须从扫描之前开始，到输入封口、全部 ingest/图/向量/store 关闭、pending 账本确认、
checkpoint 确认及客户端同步返回之后结束。不得仅以 accepted 或某个中间批次的 pending=0 截止。
