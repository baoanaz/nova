# AI3：图／SQL 批处理正确性与恢复审查

基点：`2ce75abf5c8573a6b445143b7bda88f56d1d511e`。
审查提交：`ecbb582`、`2ce75ab` 及直接依赖。
分支：`review/ai3-recovery-2ce75ab`；worktree：`/root/xuwenzheng/ace/nova-ai3-review`。

只新增本报告和 `core/tests/storage/test_ai3_recovery_review.py`，不修改生产实现、主工作区或冻结测量版本。

## 主线接收与修复

本报告记录的是 `2ce75ab` 的审查结果。主负责人接收后已修复 AI3-4：
两个新增图查询均按连接实际参数上限分窗，移除了这两个用例的 xfail。
复测本文件与 graph/sql batch 测试：**17 passed，4 xfailed**。
剩余四个 xfail 对应 AI3-1/2/3 的原有缺陷；本轮为保持原有图结果，不扩展 schema
或修改既有图身份语义。以下原始复现记录保留，不能把它误读为修复后的结果。

## 结论

发现 4 项问题，提供 6 个期望行为复现用例：前 3 项来自两个提交之前已有的直接依赖，不能归因为本次性能优化新增；第 4 项是 `ecbb582` 批查询在降低 SQLite 参数上限时的回归。未在本次定向验证中发现 `2ce75ab` 的 FTS rowid 绑定或单文件回滚回归。

这不是完整性能／恢复验收。测试只涉及临时 SQLite 库、最小单元 fixture，不读取或替换 LangChain 性能语料、向量 fixture，不调用 embedding，不联网，不编译 Rust，不运行完整或并行重测试。

独立复核限制：已调用 advisor 接口，请求只读复核交付物及上述直接依赖；请求在 30 秒后超时，未获得第二审阅者结论。以下发现依据本地代码证据和实际复现。

## AI3-1 · P1：同名来源的更新／删除会清掉其他文件的出边（原有缺陷）

位置：`core/nova_core/storage/store.py:397`、`:402`、`:537`；`storage/schema.sql` 的 `edges` 表。

`symbols` 以包含文件路径的 ID 区分符号，但 `edges.source` 只有 FQN。文件更新和删除执行 `DELETE FROM edges WHERE source = ?`，无法区分另一文件中的同名符号。

复现：a.py、b.py 都定义 `Worker.run`，已有分别指向 `a.target`、`b.target` 的边。替换 b.py 后仅剩 `b.changed`，删除 b.py 后所有边消失，a.py 的出边均丢失。只重试 b.py 无法恢复 a.py 的图信息。

用例：`test_namesake_file_keeps_other_files_edges[replace/delete]`。

建议：主负责人先明确边的文件／符号身份，再调整插入、去重、清理和读取；仅减少 DELETE 范围无法完整解决缺少来源身份的问题。这可能涉及 schema 契约，不建议混进纯批处理优化。

## AI3-2 · P1：跨文件引用被当作重复项丢弃（原有缺陷）

位置：`core/nova_core/storage/store.py:1139`，`_insert_one_unresolved`。

去重键只有 `(from_symbol, reference_name, reference_kind, IFNULL(line,-1))`，没有 `file_path`。两个文件中的同名函数在相同行调用同名目标时，第二文件的引用不入库。解析器虽然支持同文件优先匹配，也拿不到被丢弃的引用。

复现：a.py、b.py 中 `Worker.run` 都在第 2 行调用 `helper`，各自的局部目标分别是 `A.helper`、`B.helper`。`resolve_graph` 后仅有 `A.helper`，没有 `B.helper`；第二条引用也不存在，failed 重试无从恢复。

用例：`test_namesake_refs_resolve_to_each_files_local_symbol`。

建议：引用去重加入来源文件身份，并与 AI3-1 的图身份修复协调；保留跨文件的引用不能单独解决后续删边冲突。

## AI3-3 · P2：重定向吞掉非唯一键错误并删掉原边（原有缺陷）

位置：`core/nova_core/storage/store.py:715`，`retarget_edges`。

所有 `sqlite3.IntegrityError` 均被解释为“目标边已存在”，随后删除原边。只有确认是边身份唯一键冲突时，删除旧边才等价于完成重定向。

复现：用 `BEFORE UPDATE ON edges` 触发器注入 `RAISE(ABORT, ...)`。调用返回而不抛异常，原裸名边被删除，新目标边没有写入；后续重试没有输入。触发器只是确定性故障注入，当前冻结 schema 本身没有这条触发器，不声称这是正常输入下的新增故障。

用例：`test_retarget_abort_preserves_edge_for_retry`。

建议：只处理明确的唯一键冲突，且确认目标身份确实存在；其他完整性异常应传播并回滚、保留原边。

## AI3-4 · P2：新增图批查询没有服从实际参数上限（新增、条件触发）

位置：`core/nova_core/storage/store.py:642`、`:729`。

`resolve_ref_targets` 和 `add_spec_refs` 以固定 500 个参数分窗；`2ce75ab` 的 `_insert_rows` 已读取 `Connection.getlimit`，图查询却没有相同保护。

复现：连接执行 `setlimit(SQLITE_LIMIT_VARIABLE_NUMBER, 80)` 后，81 条 ref 或 81 个 spec symbol 导致 `OperationalError: too many SQL variables`。旧逐条实现每条只用少量参数，不受这个窗口问题影响。默认 SQLite 参数上限高于 500，本报告不把它描述为默认环境故障。异常不会造成部分提交，但不调整窗口的原样重试会再次失败。

用例：`test_graph_batches_respect_actual_variable_limit[targets/specs]`。

建议：图查询也按 `min(500, conn.getlimit(...))` 分窗；另核对其他查询的固定参数预算。

## 已通过的定向验证

结合原有 `test_graph_batch.py`、`test_sql_batches.py`：

- 多义边重复请求、缺失 ref、NULL 行号去重、已有 provenance 保持、重复重试。
- 图插入失败整批回滚，关库重开后重试；外层事务回滚。
- spec pair 去重、stale/provenance 保持、失败回滚和重试。
- SQL 多窗口插入、乱序 RETURNING、无 RETURNING 分支的 FTS rowid 对齐。
- 后续窗口约束失败恢复旧文件，再次提交成功。
- 新增：删除已解析 ref 时失败，回滚内层保存点，保留外层已写数据和原引用；清除故障后可重试。
- 新增：4 个子进程 `os._exit(73)` 场景，分别覆盖文件／图写入在外层事务未提交和已经提交时直接退出；重开检查旧／新状态及重试，文件场景同时检查 chunks、FTS 对齐、文件 hash 和完整性。

进程退出测试不等于断电持久性测试；当前连接为 WAL + `synchronous=NORMAL`。没有验证磁盘故障、COMMIT/RELEASE 自身失败、SQL／向量跨库原子性或全流水线 readiness。

## 执行方法与结果

使用现有 Python 环境，`PYTHONPATH` 明确指向隔离 worktree，关闭字节码／pytest 缓存写入。下列命令需在该 worktree 内执行：

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/core" \
/root/xuwenzheng/ace/nova/.venv/bin/python -m pytest \
-c /dev/null -p no:cacheprovider -q \
core/tests/storage/test_ai3_recovery_review.py \
core/tests/storage/test_graph_batch.py \
core/tests/storage/test_sql_batches.py
```

结果：**15 passed, 6 xfailed**。6 个 strict xfail 明确登记上述未修复缺陷；不是 21 项全部正确。修复后 XPASS 会要求移除对应 xfail。另用 `--runxfail` 实际观察到 6 个预期失败：两项误删边、一项遗漏目标、一项重定向失败丢边、两项参数超限。

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/core" \
/root/xuwenzheng/ace/nova/.venv/bin/python -m pytest \
-c /dev/null -p no:cacheprovider -q --runxfail \
core/tests/storage/test_ai3_recovery_review.py \
-k 'namesake or retarget_abort or actual_variable_limit'
```

此命令预期以退出码 1 显示 **6 failed, 5 deselected**，供主负责人逐项修复时核对。
