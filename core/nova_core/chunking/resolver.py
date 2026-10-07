"""unresolved 两阶段解析 + 裸名边 fqn 化 + spec_references 匹配（TASK-006）。

设计依据：``docs/design/Module/01-切片存储.md`` §2.2 / §4.1（全局二遍解析）、
``docs/design/Background/02-codegraph.md`` §4（pending → resolved(删) / failed(保留重试)）；
实现期口径：``docs/contracts/PROCESS.md`` §3.2 R5（边按 source 归属、spec 引用重挂）、
R8（imports 边 target_name 形态与相对导入语义由本卡处理）。

算法口径（评审关键）：

1. **候选匹配**：先按 fqn 精确匹配（``symbols.fqn == name``），否则按 ``name_tail``
   （``A::b`` / ``a.b`` → ``b``）匹配符号短名；两类查询都走 ``Store.exact_symbols``。
2. **收敛顺序**（逐级过滤，任一阶段只剩 1 个即命中）：
   imports 类引用的推定模块路径 → 同文件 → ``is_exported``；仍多个 → 全部连边
   （``provenance='synthesized'``）并记入 :class:`AmbiguousRef`；无命中 →
   :meth:`Store.mark_refs_failed`（``status='failed'`` 保留，供 :func:`retry_failed` 重试）。
3. **imports 的路径推定**：``from .service import Service`` / ``from pkg.mod import name`` 等
   先按"末段是符号名、其余是模块路径"推定候选文件（相对导入按源文件目录回溯前导点的层数），
   候选符号必须落在推定文件上（支持 ``src/`` 布局：按路径后缀匹配）；推定失败即判 failed，
   **不跨文件猜同名符号**（宁缺勿假）。C/C++ ``#include`` 边（target 是 ``resolve_include``
   产出的文件路径）与系统头、通配符导入不参与符号解析，保持原样（见 ``edges_skipped``）。
4. **裸名边**（``Store.unresolved_edges``，如 Python/C/C++ 抽取器写入的裸名 calls）：
   只有收敛到唯一 fqn 才 ``retarget_edges``；多义时不猜（``retarget_edges`` 是"移动"语义，
   一裸名边只能落一个 fqn），仅记入报告。全连语义只适用于 pending ref（此时一条 ref 可落多条边）。
5. **spec_references**：``SpecBlockDef.mentioned`` 归一化（去反引号/去调用括号、排除路径形态）后
   匹配符号：fqn 精确优先，否则同名全部挂上（宁多勿漏，D-06 弱引用）；``provenance`` 恒
   ``inferred``（由 ``Store.add_spec_refs`` 强制），符号删除/改名的级联 ``stale`` 归 TASK-001 路径。

Store API 边界（已记入任务卡执行记录）：``Store.resolve_refs`` 对同一条 ref 只落一条边
（写边后立即删行），因此"全连"由本模块对额外目标重新播种 pending 引用再解析实现；
``Store`` 目前没有"直接批量加边"的原语，若要更干净的实现需走 L2 契约扩展。
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from nova_core.chunking.splitter import spec_block_id
from nova_core.parsing.registry import detect_language
from nova_core.storage import (
    EdgeRow,
    EdgeTargetUpdate,
    RefResolution,
    Store,
    SymbolRow,
    UnresolvedRefRow,
)
from nova_core.types import ParsedFile, UnresolvedRef

__all__ = [
    "AmbiguousRef",
    "ResolveReport",
    "link_spec_references",
    "name_tail",
    "resolve_edges",
    "resolve_pending",
    "retry_failed",
]

#: 走"模块路径推定"的引用 kind（其余 kind 只按同文件/导出收敛）。
_IMPORT_KINDS = frozenset({"import", "imports"})

#: 可作为符号引用的名字形态：标识符 + ``::``/``.`` 链（可带相对导入前导点）。
_NAME_SHAPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:(?:::|\.)[A-Za-z_][A-Za-z0-9_]*)*$")


@dataclass(frozen=True, slots=True)
class AmbiguousRef:
    """未收敛到唯一符号的引用（供 Missing Evidence 复用；``origin`` 区分来源表）。"""

    from_symbol: str
    name: str
    kind: str
    candidates: tuple[str, ...]  # 候选符号 id（多义全连时即为实际连边的目标）
    origin: str                  # pending / failed / edge
    ref_id: int | None = None


@dataclass(frozen=True, slots=True)
class ResolveReport:
    """一次解析动作的结果（TASK-007 汇总进 IngestReport / 日志）。"""

    resolved: int = 0            # pending/failed 引用落边条数（多义全连时一条引用贡献多条）
    failed: int = 0              # 本次被置为 failed 的引用条数
    retried: int = 0             # retry_failed 实际检查的 failed 行数
    edges_retargeted: int = 0    # 裸名边重定向成功的行数
    edges_unresolved: int = 0    # 尝试过但无命中的裸名边
    edges_skipped: int = 0       # 非符号目标（文件路径/系统头/通配符）而跳过的裸名边
    spec_refs: int = 0           # 新写入的 spec_references 行数
    ambiguous: tuple[AmbiguousRef, ...] = ()


def name_tail(name: str) -> str:
    """``util.greet`` → ``greet``；``A::refresh`` → ``refresh``（口径同 Store 的 name_tail）。"""
    return name.replace("::", ".").split(".")[-1]


# ---------------------------------------------------------------------------
# 对外入口
# ---------------------------------------------------------------------------


def resolve_pending(
    store: Store, *, file_paths: Sequence[str] | None = None
) -> ResolveReport:
    """解析全部 ``status='pending'`` 引用（索引后调用）；``file_paths`` 可限定文件子集。"""
    rows = store.unresolved_refs(status="pending")
    if file_paths is not None:
        wanted = set(file_paths)
        rows = [row for row in rows if row.file_path in wanted]
    return _resolve_rows(store, rows, origin="pending")


def retry_failed(store: Store, new_symbol_names: Sequence[str]) -> ResolveReport:
    """新符号出现后只重试相关 ``failed`` 引用（按 ``name_tail`` 命中的才重试）。"""
    keys = {name_tail(name) for name in new_symbol_names}
    keys |= {name for name in new_symbol_names}
    if not keys:
        return ResolveReport()
    rows = [
        row
        for row in store.unresolved_refs(status="failed")
        if row.name_tail in keys or row.reference_name in keys
    ]
    report = _resolve_rows(store, rows, origin="failed")
    return ResolveReport(
        resolved=report.resolved,
        failed=report.failed,
        retried=len(rows),
        ambiguous=report.ambiguous,
    )


def resolve_edges(store: Store, *, kinds: Sequence[str] | None = None) -> ResolveReport:
    """把裸名边（target 尚未落成 fqn）重定向到唯一命中的符号 fqn。"""
    index = _SymbolIndex(store)
    updates: list[EdgeTargetUpdate] = []
    ambiguous: list[AmbiguousRef] = []
    unresolved = 0
    skipped = 0
    for edge in store.unresolved_edges(kinds):
        if not _is_symbol_ref(edge.target):
            skipped += 1
            continue
        targets = _match(index, edge.target, _source_file(store, index, edge.source), edge.kind)
        if not targets:
            unresolved += 1
            continue
        if len(targets) > 1:
            unresolved += 1
            ambiguous.append(_ambiguous(edge, targets))
            continue
        updates.append(
            EdgeTargetUpdate(
                source=edge.source,
                target=edge.target,
                kind=edge.kind,
                line=edge.line,
                new_target=targets[0].fqn,
            )
        )
    changed = store.retarget_edges(updates) if updates else 0
    return ResolveReport(
        edges_retargeted=changed,
        edges_unresolved=unresolved,
        edges_skipped=skipped,
        ambiguous=tuple(ambiguous),
    )


def link_spec_references(store: Store, parsed_files: Sequence[ParsedFile]) -> ResolveReport:
    """把 SpecBlock 的 ``mentioned`` 匹配到符号并写 ``spec_references``（D-06 弱引用）。"""
    index = _SymbolIndex(store)
    pairs: list[tuple[str, str]] = []
    for parsed in parsed_files:
        if parsed.fallback:
            continue
        for block in parsed.spec_blocks:
            block_id = spec_block_id(block)
            for mention in block.mentioned:
                for symbol_id in _mention_symbol_ids(index, mention):
                    pairs.append((block_id, symbol_id))
    added = store.add_spec_refs(pairs) if pairs else 0
    return ResolveReport(spec_refs=added)


# ---------------------------------------------------------------------------
# 内部：引用行解析
# ---------------------------------------------------------------------------


def _resolve_rows(
    store: Store, rows: Sequence[UnresolvedRefRow], *, origin: str
) -> ResolveReport:
    index = _SymbolIndex(store)
    singles: list[RefResolution] = []
    failed_ids: list[int] = []
    ambiguous: list[AmbiguousRef] = []
    multis: list[tuple[UnresolvedRefRow, tuple[str, ...]]] = []

    for row in rows:
        targets = _match(index, row.reference_name, row.file_path, row.reference_kind)
        if not targets:
            failed_ids.append(row.id)
        elif len(targets) == 1:
            singles.append(RefResolution(ref_id=row.id, target_fqn=targets[0].fqn))
        else:
            multis.append((row, targets))
            ambiguous.append(
                AmbiguousRef(
                    from_symbol=row.from_symbol,
                    name=row.reference_name,
                    kind=row.reference_kind,
                    candidates=tuple(candidate.id for candidate in targets),
                    origin=origin,
                    ref_id=row.id,
                )
            )

    resolved = store.resolve_refs(singles) if singles else 0
    resolved += _resolve_multi(store, multis)
    failed = store.mark_refs_failed(failed_ids) if failed_ids else 0
    return ResolveReport(resolved=resolved, failed=failed, ambiguous=tuple(ambiguous))


def _resolve_multi(
    store: Store, multis: Sequence[tuple[UnresolvedRefRow, tuple[SymbolRow, ...]]]
) -> int:
    """多义引用全连（``provenance='synthesized'``）。

    ``Store.resolve_refs`` 对一条 ref 只落一条边（写边后删行），因此除首个目标外的每个目标，
    都先按原引用签名重新播种一条 pending 行、再解析一次；净效果是 N 条边 + 0 条残留引用。
    """
    resolved = 0
    for row, targets in multis:
        first = store.resolve_refs(
            [RefResolution(ref_id=row.id, target_fqn=targets[0].fqn, provenance="synthesized")]
        )
        if not first:  # ref 行已被其它路径消费
            continue
        resolved += first
        for target in targets[1:]:
            seeded = _seed_ref_id(store, row)
            if seeded is None:
                break
            resolved += store.resolve_refs(
                [RefResolution(ref_id=seeded, target_fqn=target.fqn, provenance="synthesized")]
            )
    return resolved


def _seed_ref_id(store: Store, row: UnresolvedRefRow) -> int | None:
    """按原签名重新播种一条 pending 引用并返回其 id（找不到返回 None）。"""
    store.upsert_unresolved(
        [
            UnresolvedRef(
                from_fqn=row.from_symbol,
                name=row.reference_name,
                kind=row.reference_kind,
                line=row.line,
            )
        ],
        row.file_path,
        row.language,
    )
    signature = (row.from_symbol, row.reference_name, row.reference_kind, row.line)
    for candidate in reversed(store.unresolved_refs(status="pending", file_path=row.file_path)):
        if (
            candidate.from_symbol,
            candidate.reference_name,
            candidate.reference_kind,
            candidate.line,
        ) == signature:
            return candidate.id
    return None


# ---------------------------------------------------------------------------
# 内部：匹配
# ---------------------------------------------------------------------------


class _SymbolIndex:
    """一次解析动作内的符号查询缓存（符号表在同一次动作中不变）。"""

    def __init__(self, store: Store) -> None:
        self._store = store
        self._cache: dict[str, list[SymbolRow]] = {}

    def by_name(self, name: str) -> list[SymbolRow]:
        cached = self._cache.get(name)
        if cached is None:
            cached = self._store.exact_symbols(name, limit=None)
            self._cache[name] = cached
        return cached


def _match(
    index: _SymbolIndex, name: str, source_file: str | None, kind: str
) -> tuple[SymbolRow, ...]:
    """返回候选符号行（() = 无命中；1 个 = 唯一命中；多个 = 多义）。"""
    if not _is_symbol_ref(name):
        return ()
    rows = [row for row in index.by_name(name) if row.fqn == name]
    if not rows:
        tail = name_tail(name)
        rows = [row for row in index.by_name(tail) if row.name == tail or row.fqn == tail]
    if not rows:
        return ()

    if kind in _IMPORT_KINDS:
        # 导入目标必须落在推定模块上（先于"唯一命中"短路）：宁缺勿假
        hints = _import_path_hints(source_file, name)
        if hints:
            rows = [row for row in rows if _hint_match(row.file_path, hints)]
            if not rows:
                return ()

    if len(rows) == 1:
        return (rows[0],)

    if source_file:
        same_file = [row for row in rows if row.file_path == source_file]
        if len(same_file) == 1:
            return (same_file[0],)
        if same_file:
            rows = same_file

    exported = [row for row in rows if row.is_exported]
    if len(exported) == 1:
        return (exported[0],)
    if exported:
        rows = exported

    return tuple(_dedupe_rows(rows))


def _dedupe_rows(rows: Sequence[SymbolRow]) -> list[SymbolRow]:
    """去重：同一符号 id 只留一次；fqn 相同（跨文件同名）保留第一条（边表按 fqn 唯一）。"""
    seen: set[str] = set()
    unique: list[SymbolRow] = []
    for row in rows:
        if row.id in seen:
            continue
        seen.add(row.id)
        unique.append(row)
    return unique


def _is_symbol_ref(name: str) -> bool:
    """名字是否可能是符号引用（排除路径/系统头/通配符/表达式形态）。"""
    text = name.strip()
    if not text:
        return False
    if "/" in text or text.startswith("<") or text.endswith("*"):
        return False
    if detect_language(text) is not None:  # 带已知扩展名 → 文件路径
        return False
    stripped = text.lstrip(".")  # 相对导入的前导点
    return bool(stripped) and _NAME_SHAPE_RE.match(stripped) is not None


def _source_file(store: Store, index: _SymbolIndex, source: str) -> str | None:
    """边的 source 侧文件：模块级边直接用文件路径，符号级边查符号归属文件。"""
    if not source:
        return None
    if detect_language(source) is not None:
        return source
    for row in index.by_name(source):
        if row.fqn == source and row.file_path:
            return row.file_path
    return None


def _import_path_hints(source_file: str | None, name: str) -> tuple[str, ...]:
    """imports 引用的推定模块文件（repo 相对路径）；推不出返回 ()。"""
    if not source_file:
        return ()
    dots = len(name) - len(name.lstrip("."))
    parts = [part for part in name[dots:].replace("::", ".").split(".") if part]
    if not parts:
        return ()
    hints: list[str] = []
    if dots:
        package = _trim_dirs(_dirname(source_file), dots - 1)
        if len(parts) == 1:
            # from . import x：x 可能是包内名字（__init__.py）或子模块
            hints.append(_join(package, "__init__.py"))
            hints.append(_join(package, parts[0]) + ".py")
            hints.append(_join(_join(package, parts[0]), "__init__.py"))
        else:
            module = _join(package, "/".join(parts[:-1]))
            hints.append(module + ".py")
            hints.append(_join(module, "__init__.py"))
    else:
        module = "/".join(parts[:-1])
        if module:
            hints.append(module + ".py")
            hints.append(_join(module, "__init__.py"))
    return tuple(dict.fromkeys(hint for hint in hints if hint))


def _hint_match(path: str | None, hints: Sequence[str]) -> bool:
    """候选文件是否命中推定模块（允许 ``src/`` 之类的前缀布局：按后缀匹配）。"""
    if not path:
        return False
    return any(path == hint or path.endswith("/" + hint) for hint in hints)


def _dirname(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def _trim_dirs(path: str, levels: int) -> str:
    for _ in range(max(0, levels)):
        path = _dirname(path)
    return path


def _join(left: str, right: str) -> str:
    return f"{left}/{right}" if left else right


def _ambiguous(edge: EdgeRow, targets: tuple[SymbolRow, ...]) -> AmbiguousRef:
    return AmbiguousRef(
        from_symbol=edge.source,
        name=edge.target,
        kind=edge.kind,
        candidates=tuple(candidate.id for candidate in targets),
        origin="edge",
    )


# ---------------------------------------------------------------------------
# 内部：spec_references 匹配
# ---------------------------------------------------------------------------


def _mention_symbol_ids(index: _SymbolIndex, mention: str) -> tuple[str, ...]:
    """一个 ``mentioned`` 候选 → 符号 id 列表（fqn 精确优先，否则同名全挂）。"""
    name = _normalize_mention(mention)
    if name is None:
        return ()
    direct = [row for row in index.by_name(name) if row.fqn == name]
    if direct:
        return tuple(dict.fromkeys(row.id for row in direct))
    tail = name_tail(name)
    return tuple(
        dict.fromkeys(
            row.id
            for row in index.by_name(tail)
            if row.name == tail or row.fqn == tail
        )
    )


def _normalize_mention(mention: str) -> str | None:
    """行内 code / 符号词 → 符号候选名；路径与表达式形态返回 None。"""
    text = mention.strip().strip("`").strip()
    if not text:
        return None
    if "(" in text:  # ``refresh()`` / ``f(a, b)`` → 取被调名
        text = text.split("(", 1)[0].strip()
    text = text.strip("\"'`").strip()
    if not text or not _NAME_SHAPE_RE.match(text):
        return None
    if "/" in text or detect_language(text) is not None:
        return None
    return text
