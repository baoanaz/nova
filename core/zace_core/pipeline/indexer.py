"""索引流水线：``ChangeSet`` → 增量失效 → 向量对账（TASK-007）。

设计依据：``docs/design/Module/01-切片存储.md`` §4.1（双层增量）、§4.2（配置指纹与分层失效）、
§4.3（时序）；实现期口径：``docs/contracts/PROCESS.md`` §3.2 R1（``.h`` 仓库级抬升）、
R4（``FileDelta`` 三集合）、R8（imports 边缘）、R10（向量相似度/rebuild 语义）。

职责与边界：

- **唯一索引入口**：``Indexer.ingest(changes)`` 是 CLI（TASK-013）与 Phase 2 service 上传共用的
  同步索引路径；job 表/进度上报/并发 worker 池都不在本卡（Phase 2 / Module/01 §4.3）。
- **消费而非重写**：解析走 ``parsing.registry``，切块走 TASK-006 ``split_file``/``embedding_text``，
  二阶段解析与 spec 引用走 TASK-006 ``resolve_pending``/``retry_failed``/``resolve_edges``/
  ``link_spec_references``，入库走 TASK-001 ``Store``，向量走 TASK-009 ``VectorStore``；
  索引侧 embedding 一律 ``embed()``（passage），不得用 ``embed_query()``（R2）。
- **语言识别（R1）**：本卡是唯一知道"仓库文件集"的地方。若仓库已见任意 C++ 扩展名文件
  （本次变更集 ∪ 已索引语言 ∪ 全量扫描清单），``.h`` 按 ``cpp`` 解析；registry 不改。
  V1 已知限制（R1）：先索引 ``.h`` 后出现 ``.cpp`` 时，旧 ``.h`` 要等它下次变更才重解析。
- **三档执行**（``check_fingerprint`` 驱动，D-07）：
  ``none`` → 常规增量（只嵌 ``FileDelta.new``）；``reembed`` → 重建向量表并重嵌**存量** chunk
  （不写 SQLite）；``full_reparse`` → 遍历 provider 全部文件重跑增量 + 重建向量表。
- **向量阶段 = ``EmbeddingSink`` + ``EmbeddingPipeline``（TASK-114）**：复用判定 / 分窗嵌入 /
  向量落库 / 缓存写回都收进 ``pipeline.embedding_sink``，本类只负责"解析 + 写 SQLite + 投递"。
  嵌入在**后台消费者线程**里跑，与本地 CPU 段重叠（P1-4）；sink 任何时刻只持有一个窗口的
  向量，不再跨窗口累积整仓 ``list[float]``（P0-2，实测曾多占 824MB）。
- **整批单事务（P1-3）**：一轮文件共用一个写事务（``Store.write_batch``），每个文件仍是
  独立的 ``SAVEPOINT``——单文件失败隔离不变，省掉每文件一次 ``COMMIT``。
- **向量清理**：``FileDelta.removed_chunk_ids`` 直接删；整文件删除由
  ``Store.apply_deletions`` 在同一事务里返回被删 chunk id 后清理（TASK-REVIEW-RUNTIME
  P1-2 修掉了旧的跨进程孤儿向量缺陷：之前依赖 ``Indexer`` 进程内的 ``_known_chunks``，
  而 ``Engine`` 每次 ingest 都新建 ``Indexer``，跨调用删除就只删 SQLite 不删向量）。
  ``orphan_files`` 仍上报“本进程未见过的删除路径”，但那只是排查信号，不再影响向量清理。
- 单项目串行：不做并发（跨项目并行属 service 层职责）。
- **单文件失败隔离**（TASK-018 §C，Module/01 §4.3 per-file 韧性）：解析失败走 fallback；
  但“切分/落库”环节的意外异常只写 ``report.errors`` 并跳过该文件，**不**中断整次 ingest，
  也**不**计入 added/modified。崩溃、静默丢数据都比“如实报告后继续”更差。
- **索引范围阈值（TASK-037 §B / R43）**：``> 128 KB``（可配置）与二进制（前 8 KB 中不可打印
  字符 ``> 10%``）的文件在**读取之后、解析之前**跳过，理由记入 ``skip_reasons``（``skipped_files``
  保持“路径列表”语义不变，见 ``pipeline/ignore.py`` 的模块 docstring）。阈值判定在 Indexer 而不是
  ``DirectorySource``：service 上传路径（blob 内容已在内存）也必须走同一阈值，且跳过必须可观测。
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field

from zace_core.chunking import (
    IndexFingerprint,
    Invalidation,
    check_fingerprint,
    link_spec_references,
    resolve_edges,
    resolve_pending,
    retry_failed,
    split_file,
    stored_fingerprint,
    write_fingerprint,
)
from zace_core.hashing import file_content_hash
from zace_core.interfaces import EmbeddingProvider
from zace_core.parsing.registry import EXTENSION_LANGUAGE, detect_language, get_parser
from zace_core.pipeline.embedding_sink import EmbeddingPipeline
from zace_core.pipeline.generated import is_generated
from zace_core.pipeline.ignore import (
    SKIP_REASON_BINARY,
    IndexScope,
    oversize_reason,
)
from zace_core.pipeline.source import SourceProvider
from zace_core.storage import Store
from zace_core.types import ChangeSet, ChunkDef, ParsedFile
from zace_core.vectors import VectorStore
from zace_core.vectors.cache import EmbeddingCache

__all__ = [
    "CPP_EXTENSIONS",
    "H_EXTENSION",
    "LANGUAGES_KEY",
    "Indexer",
    "IngestReport",
]

#: ``index_config`` 中记录"仓库已见语言集合"的键（R1 抬升的持久化依据）。
LANGUAGES_KEY = "indexed_languages"
#: C/C++ 歧义扩展名（R1 抬升目标）。
H_EXTENSION = ".h"
#: C++ 扩展名集合：取自 registry（含任务卡 R1 清单的全部项，外加注册表里的 ``.c++``/``.h++``）。
CPP_EXTENSIONS = frozenset(
    extension for extension, language in EXTENSION_LANGUAGE.items() if language == "cpp"
)


@dataclass(frozen=True, slots=True)
class IngestReport:
    """一次 ``ingest`` 的结果（TASK-013 日志 / Phase 2 进度上报的字段来源）。

    前 8 个字段是任务卡规定的口径；其余为流水线自证与观测所需（本卡执行记录同步记录）。
    """

    added: int = 0                # 本次按 added 处理并写入的文件数
    modified: int = 0             # 本次按 modified 处理并写入的文件数
    deleted: int = 0              # 本次删除的文件数
    chunks_new: int = 0           # 新写入且 hash 变化、需要嵌入的 chunk 数
    chunks_reused: int = 0        # content_hash 未变（文件级对账）、向量可复用的 chunk 数
    chunks_deduped: int = 0       # 按内容命中已有向量/缓存/本轮更早窗口 → 省掉嵌入调用的 chunk 数
    chunks_removed: int = 0       # 行被移除（需要删向量）的 chunk 数
    unresolved_resolved: int = 0  # 本次落边的 unresolved 引用条数
    errors: tuple[str, ...] = ()  # 解析/切分/落库失败（单文件隔离，不中断整体 ingest）
    invalidation: Invalidation = Invalidation.NONE  # 本次执行的失效层级（D-07）
    files_parsed: int = 0         # 实际解析（含兜底切分）的文件数
    vectors_upserted: int = 0     # 写入/覆盖的向量行数
    vectors_deleted: int = 0      # 删除的向量行数
    edges_retargeted: int = 0     # 裸名边 fqn 化成功行数
    spec_refs: int = 0            # 新写入的 spec_references 行数
    ambiguous_refs: int = 0       # 多义引用条数（全连 / 裸名边未定）
    skipped_files: tuple[str, ...] = ()     # 二进制/不可解码而跳过的文件
    skip_reasons: tuple[str, ...] = ()     # 等长的 ``"path:reason"``（TASK-037 §B）
    orphan_files: tuple[str, ...] = ()      # 删除时无法枚举 chunk id（向量可能残留）的文件
    languages: tuple[str, ...] = ()         # 本次处理后仓库已见语言集合（R1 抬升输入）
    warnings: tuple[str, ...] = ()          # 非致命的可操作告警（如索引位移，TASK-114 / P2-7）


@dataclass
class _Accumulator:
    """报告累加器（``IngestReport`` 保持 frozen，便于下游安全传递）。"""

    added: int = 0
    modified: int = 0
    deleted: int = 0
    chunks_new: int = 0
    chunks_reused: int = 0
    chunks_deduped: int = 0
    chunks_removed: int = 0
    unresolved_resolved: int = 0
    invalidation: Invalidation = Invalidation.NONE
    files_parsed: int = 0
    vectors_upserted: int = 0
    vectors_deleted: int = 0
    edges_retargeted: int = 0
    spec_refs: int = 0
    ambiguous_refs: int = 0
    errors: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    skip_reasons: list[str] = field(default_factory=list)
    orphan_files: list[str] = field(default_factory=list)
    _skipped_seen: set[str] = field(default_factory=set)
    languages: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)

    def skip(self, path: str, reason: str) -> None:
        """记录一个被跳过（未索引）的文件与原因（TASK-037 §B / R43）。

        ``skipped_files`` 只放路径（既有契约：service 当作 ``skipped: [path]`` 返回，
        ``service/tests/test_sync_api.py`` 精确断言），原因进平行的 ``skip_reasons``。

        **幂等**：``full_reparse`` 会枚举两次清单（``_collect_inputs`` 解析一轮，
        ``_rebuild_vectors`` 为取回存量 chunk id 再枚举一轮），同一路径会被判两次。
        重复条目会让"按原因分组"统计翻倍，因此按路径去重（保留首次的原因）。
        """
        if path in self._skipped_seen:
            return
        self._skipped_seen.add(path)
        self.skipped_files.append(path)
        self.skip_reasons.append(f"{path}:{reason}")

    def report(self) -> IngestReport:
        return IngestReport(
            added=self.added,
            modified=self.modified,
            deleted=self.deleted,
            chunks_new=self.chunks_new,
            chunks_reused=self.chunks_reused,
            chunks_deduped=self.chunks_deduped,
            chunks_removed=self.chunks_removed,
            unresolved_resolved=self.unresolved_resolved,
            errors=tuple(self.errors),
            invalidation=self.invalidation,
            files_parsed=self.files_parsed,
            vectors_upserted=self.vectors_upserted,
            vectors_deleted=self.vectors_deleted,
            edges_retargeted=self.edges_retargeted,
            spec_refs=self.spec_refs,
            ambiguous_refs=self.ambiguous_refs,
            skipped_files=tuple(self.skipped_files),
            skip_reasons=tuple(self.skip_reasons),
            orphan_files=tuple(self.orphan_files),
            languages=self.languages,
            warnings=tuple(self.warnings),
        )


@dataclass(frozen=True, slots=True)
class _Input:
    """一个待索引文件（``kind`` 只影响报告计数）。"""

    path: str
    data: bytes
    kind: str  # "added" / "modified"


@dataclass(frozen=True, slots=True)
class _Indexed:
    """单文件索引结果（供后续二阶段解析与向量阶段复用）。"""

    parsed: ParsedFile
    chunks: tuple[ChunkDef, ...]
    new_ids: tuple[str, ...]
    removed_ids: tuple[str, ...]


class Indexer:
    """把变更集变成索引更新的同步流水线（per-project 单写者）。"""

    def __init__(
        self,
        store: Store,
        embedding: EmbeddingProvider,
        vectors: VectorStore,
        source: SourceProvider,
        *,
        scope: IndexScope | None = None,
        embedding_cache: EmbeddingCache | None = None,
        workers: int | None = None,
    ) -> None:
        self._store = store
        self._embedding = embedding
        self._vectors = vectors
        self._source = source
        #: 索引范围阈值（TASK-037 §B / R43）：默认 ``IndexScope.from_env()``，
        #: 即 128 KB / 10% 可配置阈值（``ZACE_MAX_FILE_BYTES`` / ``ZACE_BINARY_RATIO``）。
        self._scope = scope if scope is not None else IndexScope.from_env()
        self._languages = _load_languages(store)
        #: path → 本进程写入过的 chunk id（整文件删除时清向量；见模块 docstring 的边界说明）
        self._known_chunks: dict[str, tuple[str, ...]] = {}
        #: 跨项目 embedding 缓存（TASK-111）：让同内容在不同分支/项目间复用向量。
        #: 为 ``None`` 时退化为"只在本项目内复用"（单测与老调用点）。
        self._cache = embedding_cache
        #: 向量阶段消费者线程数（TASK-115）：``None`` → ``ZACE_EMBED_WORKERS``（默认 2）。
        #: 显式传入是给测试与对照实验用的接缝（不想让用例依赖环境变量）。
        self._workers = workers

    # ------------------------------------------------------------------ 对外

    @property
    def languages(self) -> tuple[str, ...]:
        """仓库已见语言集合（R1 抬升输入）。"""
        return tuple(sorted(self._languages))

    @property
    def scope(self) -> IndexScope:
        """本索引器使用的大小/二进制阈值（TASK-037 §B）。"""
        return self._scope

    def fingerprint(self) -> IndexFingerprint:
        return IndexFingerprint.build(self._embedding.profile)

    def ingest(self, changes: ChangeSet) -> IngestReport:
        """按配置指纹决定执行档位并完成一次索引（唯一入口）。"""
        invalidation = check_fingerprint(self._store, self.fingerprint())
        if invalidation is Invalidation.FULL_REPARSE:
            return self.full_reparse(changes)
        if invalidation is Invalidation.REEMBED:
            return self.reembed(changes)
        return self._run(changes, Invalidation.NONE)

    def full_reparse(self, changes: ChangeSet | None = None) -> IngestReport:
        """指纹一级失效：遍历 provider 全部文件重跑增量 + 重建向量表。"""
        return self._run(changes or ChangeSet(), Invalidation.FULL_REPARSE)

    def reembed(self, changes: ChangeSet | None = None) -> IngestReport:
        """指纹二级失效：只重建向量（SQLite 索引原样）。"""
        return self._run(changes or ChangeSet(), Invalidation.REEMBED)

    # ------------------------------------------------------------------ 主流程

    def _run(self, changes: ChangeSet, invalidation: Invalidation) -> IngestReport:
        acc = _Accumulator(invalidation=invalidation)
        rebuild_vectors = invalidation is not Invalidation.NONE
        deleted = set(changes.deleted)

        if deleted:
            self._delete_files(sorted(deleted), acc)

        inputs = self._collect_inputs(changes, invalidation, deleted, acc)
        repo_languages = self._languages | {
            language
            for language in (detect_language(item.path) for item in inputs)
            if language is not None
        }
        repo_is_cpp = "cpp" in repo_languages

        # 指纹二级失效：先原子重建（清空）向量表，之后的写入都落在新表上。
        if rebuild_vectors:
            self._vectors.rebuild(self._embedding.profile.dim)

        indexed: list[_Indexed] = []
        removed_ids: list[str] = []
        with EmbeddingPipeline(
            self._embedding, self._vectors, cache=self._cache, workers=self._workers
        ) as pipeline:
            # 一轮文件共用一个写事务；每文件的失败隔离由内层 SAVEPOINT 保证（Store.write_batch）。
            with self._store.write_batch():
                for item in inputs:
                    result = self._index_file(item, repo_is_cpp, acc)
                    if result is None:
                        continue
                    indexed.append(result)
                    acc.chunks_reused += len(result.chunks) - len(result.new_ids)
                    # 重建后的新表也按文件立即投递；补嵌阶段只处理尚未投递的存量文件。
                    pipeline.submit(result.chunks)
                    if not rebuild_vectors:
                        removed_ids.extend(result.removed_ids)
                if rebuild_vectors:
                    self._rebuild_vectors(
                        acc, pipeline, {result.parsed.path for result in indexed}
                    )
            pipeline.flush_pending()
            # 图解析只依赖已提交的 SQLite 数据，可和向量尾窗的网络/落库重叠。
            self._resolve(acc, [result.parsed for result in indexed])
        # 删除必须等向量阶段排空：行漂移后的**按内容复用**要读旧行（旧 id 同内容），
        # 先删就会把复用来源删掉、整文件重嵌（实测踩过）。
        if removed_ids:
            acc.vectors_deleted += self._vectors.delete(removed_ids)

        stats = pipeline.stats
        acc.vectors_upserted += stats.upserted
        # ``chunks_reused`` 只统计文件级对账（FileDelta）的 hash 未变；sink 的内容复用单独计数，
        # 两者的口径互斥，且满足 ``chunks_new + chunks_reused == embedded + deduped``。
        acc.chunks_deduped += stats.deduped

        self._languages = repo_languages | {
            result.parsed.language for result in indexed if result.parsed.language
        }
        self._store.set_config(LANGUAGES_KEY, json.dumps(sorted(self._languages)))
        acc.languages = tuple(sorted(self._languages))
        if rebuild_vectors or stored_fingerprint(self._store) is None:
            write_fingerprint(self._store, self.fingerprint())
        return acc.report()

    # ------------------------------------------------------------------ 输入

    def _collect_inputs(
        self, changes: ChangeSet, invalidation: Invalidation, deleted: set[str], acc: _Accumulator
    ) -> list[_Input]:
        """待处理文件清单：全量模式先铺 provider 清单，再叠加变更集（同路径以变更集为准）。"""
        items: dict[str, _Input] = {}
        if invalidation is Invalidation.FULL_REPARSE:
            for path in self._source.list_files():
                if path in deleted:
                    continue
                # 大小预判：能拿到元数据时**不读内容**就拦掉超限文件（TASK-036 §C.1 的代价：
                # 308 MB 文件整份读入内存）。拿不到 size 的 provider 继续往下走，由下面兜底。
                size = self._source_size(path)
                if size is not None:
                    readable, reason = self._scope.should_read(path, size)
                    if not readable:
                        acc.skip(path, reason or oversize_reason(size))
                        continue
                data = _safe_read(self._source, path, acc)
                if data is None:
                    continue
                items[path] = _Input(path=path, data=data, kind="modified")
        for blob in changes.added:
            items[blob.path] = _Input(path=blob.path, data=blob.content, kind="added")
        for blob in changes.modified:
            items[blob.path] = _Input(path=blob.path, data=blob.content, kind="modified")
        return [items[path] for path in sorted(items)]

    def _source_size(self, path: str) -> int | None:
        """``source.file_size(path)``（可选协议）；不支持时返回 ``None``（安全降级）。"""
        probe = getattr(self._source, "file_size", None)
        if probe is None:
            return None
        try:
            return probe(path)
        except Exception:  # noqa: BLE001 - 元数据探测失败不该影响索引（继续走 read 兜底）
            return None

    def _index_file(self, item: _Input, repo_is_cpp: bool, acc: _Accumulator) -> _Indexed | None:
        # §B 阈值（R43）：大小在读取**之前**可判（``_collect_inputs`` 已按 ``_safe_read`` 拿到字节，
        # 这里用真实长度即可），二进制需要内容——两者都必须在"入库"之前拦掉，否则噪声文件既吃
        # 解析时间又进检索池。
        readable, size_reason = self._scope.should_read(item.path, len(item.data))
        if not readable:
            acc.skip(item.path, size_reason or oversize_reason(len(item.data)))
            return None
        decodable, binary_reason_value = self._scope.check_bytes(item.data)
        if not decodable:
            acc.skip(item.path, binary_reason_value or SKIP_REASON_BINARY)
            return None
        text = _decode(item.data)
        if text is None:
            acc.skip(item.path, SKIP_REASON_BINARY)
            return None
        language = _language_for(item.path, repo_is_cpp)
        parsed = self._parse(item.path, text, language, acc)
        try:
            chunks = tuple(split_file(parsed, text))
            delta = self._store.apply_file_change(
                parsed,
                chunks,
                file_content_hash(item.data),
                generated=is_generated(item.path, text),
            )
        except Exception as exc:  # 单文件切分/落库失败 → 如实记录并跳过（TASK-018 §C）
            acc.errors.append(f"{item.path}: {type(exc).__name__}: {exc}")
            return None
        if item.kind == "added":
            acc.added += 1
        else:
            acc.modified += 1
        acc.files_parsed += 1
        acc.chunks_new += len(delta.new_chunk_ids)
        acc.chunks_removed += len(delta.removed_chunk_ids)
        self._known_chunks[item.path] = tuple(chunk.id for chunk in chunks)
        return _Indexed(
            parsed=parsed,
            chunks=chunks,
            new_ids=tuple(delta.new_chunk_ids),
            removed_ids=tuple(delta.removed_chunk_ids),
        )

    def _parse(
        self, path: str, text: str, language: str | None, acc: _Accumulator
    ) -> ParsedFile:
        """解析单文件：未知语言或抽取器不可用 → 兜底 ``ParsedFile``（不中断 ingest）。"""
        if language is None:
            return ParsedFile(path=path, language="fallback", fallback=True)
        try:
            parser = get_parser(language)
            parsed = parser.parse(path, text)
        except Exception as exc:  # ParserUnavailableError / 抽取器缺陷
            acc.errors.append(f"{path}: {type(exc).__name__}: {exc}")
            return ParsedFile(
                path=path,
                language=language,
                parse_errors=(f"{type(exc).__name__}: {exc}",),
                fallback=True,
            )
        if parsed.parse_errors:
            acc.errors.append(f"{path}: " + "; ".join(parsed.parse_errors))
        return parsed

    # ------------------------------------------------------------------ 删除

    def _delete_files(self, paths: Sequence[str], acc: _Accumulator) -> None:
        """整文件删除：先取被删 chunk id，再清向量。

        为什么不再依赖 ``_known_chunks``（TASK-REVIEW-RUNTIME P1-2）：那是**本进程**
        写入记录的 id，而 ``Engine`` 每次 ingest 都新建 ``Indexer``。跨调用/跨进程删除时
        内存里没有这批 id，旧行为只删 SQLite 不删 LanceDB，留下孤儿向量。
        现在改由 ``Store.apply_deletions`` 在同一事务里取 id 并返回（无 TOCTOU）。

        ``_known_chunks`` 仍保留：它用于区分“曾索引过”与“从未见过”（``orphan_files``），
        这是排查用的信号，与向量清理解耦。
        """
        for path in paths:
            if self._known_chunks.pop(path, None) is None:
                acc.orphan_files.append(path)
        removed = self._store.apply_deletions(list(paths))
        acc.deleted += len(paths)
        if removed:
            acc.vectors_deleted += self._vectors.delete(list(removed))

    # ------------------------------------------------------------------ 向量

    def _rebuild_vectors(
        self,
        acc: _Accumulator,
        pipeline: EmbeddingPipeline,
        processed: set[str],
    ) -> None:
        """补嵌尚未处理的存量文件（reembed / full_reparse）。

        向量表已由 :meth:`_run` 重建，已处理文件在解析循环中投递过，不重复投递。
        其余按 provider 清单重新解析切分拿到 chunk id（**不写 SQLite**），
        再用 ``Store.chunks_by_ids`` 取回库内权威内容。解析口径由指纹保证。
        """
        ids: list[str] = []
        for path in self._source.list_files():
            if path in processed:
                continue
            size = self._source_size(path)
            if size is not None:
                readable, reason = self._scope.should_read(path, size)
                if not readable:
                    acc.skip(path, reason or oversize_reason(size))
                    continue
            data = _safe_read(self._source, path, acc)
            if data is None:
                continue
            # §B 阈值同样适用于重建路径：被跳过的文件不会进向量表（否则同一文件的
            # "该不该索引"在增量/全量两条路径下结论不一致）。
            readable, size_reason = self._scope.should_read(path, len(data))
            if not readable:
                acc.skip(path, size_reason or oversize_reason(len(data)))
                continue
            decodable, binary_reason_value = self._scope.check_bytes(data)
            if not decodable:
                acc.skip(path, binary_reason_value or SKIP_REASON_BINARY)
                continue
            text = _decode(data)
            if text is None:
                acc.skip(path, SKIP_REASON_BINARY)
                continue
            language = _language_for(path, "cpp" in self._languages)
            parsed = self._parse(path, text, language, acc)
            try:
                ids.extend(chunk.id for chunk in split_file(parsed, text))
            except Exception as exc:  # 同上：单文件切分失败不拖垮重建（TASK-018 §C 同一口径）
                acc.errors.append(f"{path}: {type(exc).__name__}: {exc}")
                continue
        pipeline.submit(self._store.chunks_by_ids(ids))

    # ------------------------------------------------------------------ 二阶段解析

    def _resolve(self, acc: _Accumulator, parsed_files: Sequence[ParsedFile]) -> None:
        pending = resolve_pending(self._store)
        names = [
            symbol.name for parsed in parsed_files for symbol in parsed.symbols
        ] + [symbol.fqn for parsed in parsed_files for symbol in parsed.symbols]
        retried = retry_failed(self._store, names) if names else None
        edges = resolve_edges(self._store)
        specs = link_spec_references(self._store, parsed_files)

        acc.unresolved_resolved += pending.resolved
        if retried is not None:
            acc.unresolved_resolved += retried.resolved
        acc.edges_retargeted += edges.edges_retargeted
        acc.spec_refs += specs.spec_refs
        acc.ambiguous_refs += len(pending.ambiguous) + len(edges.ambiguous)
        if retried is not None:
            acc.ambiguous_refs += len(retried.ambiguous)


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _safe_read(source: SourceProvider, path: str, acc: _Accumulator) -> bytes | None:
    """读一个文件并**隔离任何失败**：异常记入 ``acc.errors`` 后返回 ``None``。

    TASK-036 §B 复现的崩溃（最小复现见
    ``core/tests/integration/test_ingest_isolation.py``）：``DirectorySource.list_files()``
    会把文件名含反斜杠的路径列出来（反斜杠在 Linux 上是合法文件名字符，老仓库里存在），
    而它的 ``read()`` 用 :class:`SourcePathError`（``ValueError``）拒绝这类路径。此前只有
    ``OSError`` 被捕获——**一个这样的文件就让整次 ingest 中止**，连已解析的文件都留不下来。

    为什么放宽到整个 ``Exception``：隔离本身是设计行为（Module/01 §4.3 per-file 韧性、
    TASK-018 §C 同一口径），“如实记录并跳过”严格优于“整体中止”；``KeyboardInterrupt`` 等
    ``BaseException`` 不受影响。读写两侧路径口径的一致性归 TASK-037（``pipeline/source.py``），
    本卡只保证“不再拖垮整次 ingest”。
    """
    try:
        return source.read(path)
    except Exception as exc:  # noqa: BLE001 - 见 docstring：隔离是设计行为
        acc.errors.append(f"{path}: {type(exc).__name__}: {exc}")
        return None


def _decode(data: bytes) -> str | None:
    """字节 → 文本；二进制（含 NUL）返回 None（跳过，不产兜底块）。"""
    if b"\x00" in data:
        return None
    return data.decode("utf-8", errors="replace")


def _language_for(path: str, repo_is_cpp: bool) -> str | None:
    """语言识别 + R1 仓库级抬升：C++ 仓库里 ``.h`` 按 ``cpp`` 解析。"""
    if repo_is_cpp and path.lower().endswith(H_EXTENSION):
        return "cpp"
    return detect_language(path)


def _load_languages(store: Store) -> set[str]:
    raw = store.get_config(LANGUAGES_KEY)
    if not raw:
        return set()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return set()
    if not isinstance(parsed, list):
        return set()
    return {str(item) for item in parsed}
