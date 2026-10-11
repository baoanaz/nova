# TASK-COLDSTART-CHAIN：完整 MCP 冷启动主线（离线）

## 用户授权与范围
用户授权按判断优化 Round 6，但真实 embedding API 待后续单独授权；
Nova 只读统计/估算，不运行其冷索引。用户要求优先主线，边界硬化暂缓。

## 允许改动
- Indexer 尾窗 dispatch 提前到 write_batch 退出前；图仍必须在成功提交后。
- 词法 SQLite Store 不跨线程；Vector 与词法重叠，保持通道顺序、原超时/降级。
- Store 写入只新增计时，无 SQL 内容/schema/事务边界改造。
- db.transaction 将真实 COMMIT 纳入异常回滚保护，失败不能假成功。
- 离线 Replay 批/query 独立延迟；真实 MCP tools/call 至完整响应 JSON 解析的计时。
- 定向测试、对照证据与报告。公共接口不删改，CF-01/05/06 不修改。

## 未实施/暂缓
WAL B/C、弱化持久性、上传→ingest 重构、生产 WAL presync 默认开关、
预取代码指纹/空闲回收/有界关停。WAL A 仅临时离线单变量实验。

## 已完成实验
6 普通 + 4 延迟 + 6 WAL 的完整 MCP 样本，独立 cgroup 与固定 release 二进制，
全部逻辑/FTS/向量/规范化 Tool 指纹一致。普通没有可靠总体收益；WAL A median
仅约 0.624s/2.35%，不默认启用。完整数据与限制见
[报告](../../benches/results/coldstart-main-offline.md)。

## 交付门槛
最后修复不新增大规模性能实验，以隔离最终源码的回归与只读 review 验证；
COMMIT 真正故障后 in_transaction=False，重试结果对新连接可见；
稳定30秒和线上效果不冒充已验收。并行客户端/服务/Web改动不触碰、不纳入比较。
