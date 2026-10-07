# leveldb 基准题库（20 题）

> 靶场：`leveldb` @ `7ee830d`｜projectId `3ed886ce58bc0e47`｜索引：`/root/.nova/bench/voyage-4-lite-d1024`（复用，不重建）
> 读法：每题给了**建议工具**（`search` = 定位/事实型，`ask` = 需要跨文件综合的解释型）、**参考答案**（人工读代码核实）和**依据路径**。
> 机器可跑版本：同目录 `leveldb.jsonl`（`nova-core eval` 直接吃）。

| ID | 建议工具 | 类别 | 问题 | 参考答案 | 依据 |
|---|---|---|---|---|---|
| L-01 | `search` | symbol | MemTable 写入一条记录后，它在内存里的条目是按什么格式编码的？ | varint32(internal_key_size) + key + Fixed64((sequence << 8) | type) + varint32(value_size) + value；即 长度-键-8 字节 tag-长度-值 的拼接。 | `db/memtable.cc#MemTable::Add`<br>`db/memtable.h#MemTable` |
| L-02 | `search` | symbol | What are the skip list's maximum height and promotion probability? | kMaxHeight = 12，kBranching = 4（每层有 1/4 概率再升一层）。 | `db/skiplist.h#kMaxHeight` |
| L-03 | `search` | symbol | L0 file count 达到多少会分别触发 compaction、write slowdown 和 write stop？ | kL0_CompactionTrigger = 4 触发 compaction；kL0_SlowdownWritesTrigger = 8 开始每写延迟 1ms；kL0_StopWritesTrigger = 12 阻塞等待。 | `db/dbformat.h#kL0_CompactionTrigger` |
| L-04 | `search` | spec | sstable 文件末尾的 Footer 有多大，magic number 是多少？ | Footer 总长固定 **48 字节**：两个 BlockHandle 的编码与 padding 共 40 字节，再加 8 字节 little-endian magic；magic = 0xdb4775248b80fb57。 | `doc/table_format.md`<br>`table/format.h#Footer` |
| L-05 | `search` | spec | log（journal）文件的记录头包含哪些字段，记录类型有哪些取值？ | checksum(uint32, crc32c) + length(uint16) + type(uint8)；类型 FULL=1 / FIRST=2 / MIDDLE=3 / LAST=4，块大小 32KB。 | `doc/log_format.md` |
| L-06 | `search` | symbol | block 内部对 key 做 prefix compression 时，每条 entry 的 prefix 是什么？ | <shared><non_shared><value_size> 三个 varint，随后是 key 的非共享后缀和 value；每 block_restart_interval 条重启一次前缀共享。 | `table/block_builder.cc#BlockBuilder::Add` |
| L-07 | `search` | path | Which file implements the Bloom-filter `FilterPolicy`? | util/bloom.cc（BloomFilterPolicy，内部以 k 个探针位实现）。 | `util/bloom.cc`<br>`include/leveldb/filter_policy.h#FilterPolicy` |
| L-08 | `search` | behavior | filter block 的分区粒度 kFilterBaseLg 是多少，对应多少字节？ | kFilterBaseLg = 11，即 2^11 = 2048 字节一个分区。 | `table/filter_block.cc#kFilterBaseLg` |
| L-09 | `ask` | behavior | 一次 Get 读取会按什么顺序查找数据，快照在其中起什么作用？ | 先取 snapshot：显式 ReadOptions.snapshot 或 versions_->LastSequence()；然后依次 mem → imm（未落盘的 immutable memtable）→ current Version 的各 level。序列号决定可见性，因此快照读不受并发的后续写入影响。 | `db/db_impl.cc#DBImpl::Get` |
| L-10 | `ask` | behavior | 调用方设置 `WriteOptions.sync = true` 时，write path 上究竟发生了什么？ | AddRecord 写 journal 之后，若 status 正常且 options.sync 为真，会调用 logfile_->Sync() 强制落盘（否则只写页缓存）。 | `db/db_impl.cc#DBImpl::Write` |
| L-11 | `search` | symbol | Options 里 write_buffer_size、block_size、block_restart_interval 的默认值分别是多少？ | write_buffer_size = 4MB，block_size = 4KB，block_restart_interval = 16（另 max_open_files = 1000）。 | `include/leveldb/options.h#Options` |
| L-12 | `search` | symbol | Which function serializes a `VersionEdit` record into the MANIFEST format? | db/version_edit.cc 的 VersionEdit::EncodeTo（对应 DecodeFrom 解析）。 | `db/version_edit.cc#VersionEdit::EncodeTo` |
| L-13 | `search` | behavior | MANIFEST 文件和 CURRENT 文件的文件名是怎么拼出来的？ | 由 db/filename.cc 的 DescriptorFileName(dbname, number) 与 CurrentFileName(dbname) 生成，另有 LogFileName 等；CURRENT 指向当前 MANIFEST。 | `db/filename.cc#DescriptorFileName` |
| L-14 | `ask` | behavior | 为什么 DBImpl::Get 里要把 mem_/imm_/current 都 Ref 一遍再释放锁？ | 为了在解锁期间安全读取：Ref 保证后台 compaction / memtable 切换不会释放这些对象，读完再 Unref。Get 内部先拷贝 mem/imm/current 指针，然后 Unlock 读取，避免长时间持锁。 | `db/db_impl.cc#DBImpl::Get` |
| L-15 | `search` | symbol | 跳表查找第一个 `>= key` 的节点时调用哪个 member function？ | SkipList::FindGreaterOrEqual（Find 与迭代器都复用它）。 | `db/skiplist.h#FindGreaterOrEqual` |
| L-16 | `ask` | behavior | 一个 memtable 从写满到落成 sstable 并写入 VersionSet，中间经历哪些步骤，输出一定在 level-0 吗？ | memtable 超过 write_buffer_size → MakeRoomForWrite 切换成 imm 并新建 log → 后台 CompactMemTable → WriteLevel0Table 调 BuildTable 写出 sstable → VersionEdit::AddFile 记录目标 level → LogAndApply 写 MANIFEST → imm 释放。输出**不一定在 level-0**：若与已有文件无重叠且 grandparent 重叠量受控，PickLevelForMemTableOutput 可将其直接提升到 level 1 或 2。 | `db/db_impl.cc#DBImpl::CompactMemTable`<br>`db/db_impl.cc#DBImpl::WriteLevel0Table`<br>`db/version_set.cc#Version::PickLevelForMemTableOutput`<br>`db/builder.cc#BuildTable` |
| L-17 | `search` | path | Which file implements the two-level iteration from an sstable index block to its data blocks? | table/two_level_iterator.cc（NewTwoLevelIterator），merger.cc 负责多路归并。 | `table/two_level_iterator.cc` |
| L-18 | `search` | symbol | Arena 的 allocation strategy 是什么：block size 多大，large allocation 如何处理？ | 按 kBlockSize（4KB）为单位向系统申请块，小块在块内线性分配；大于**固定 block size 四分之一**（>1KB）的请求单独申请一个恰好大小的 block，避免浪费当前块的剩余空间。 | `util/arena.cc#Arena::AllocateFallback` |
| L-19 | `ask` | spec | leveldb 明确声明自己不具备哪些能力（不是 SQL 数据库、并发访问、客户端服务）？ | README 明确：无关系模型、不支持 SQL、无索引支持；同一时刻只能由一个进程（可多线程）访问；库本身不含 client-server，需要自己包一层服务。 | `README.md` |
| L-20 | `search` | negative | leveldb 的 Transaction（事务）类声明在哪个头文件里？ | 不存在：leveldb 没有事务 API（只有 WriteBatch 的原子批写）。该仓库内没有任何 Transaction 类。 | （无：负例） |

## 统计

- 共 20 题：`search` 14 题、`ask` 5 题、负例 1 题；语言：中文 11｜英文 4｜中英混合 5；
- 类别分布：symbol 8｜behavior 6｜path 2｜spec 3｜negative 1。
- 所有 `依据` 路径都在**被索引的文件集内**，且已用 grep 核验存在。
