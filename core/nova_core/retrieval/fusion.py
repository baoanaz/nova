"""候选去重与合并（TASK-010）：通道候选 → 统一 ``Candidate`` 池。

设计依据：``docs/design/Module/02-检索策略.md`` §4.3（融合）/§4.6（tier 语义，D-17）：

- 去重键 = ``chunk_id``（spec 块与代码块天然不同 id）；
- ``rrf_score`` 由 rrf.py 计算并写死（写入后不再变）；``score`` 初值 = ``rrf_score``
  （TASK-011 rerank 覆盖它并保持 ``rrf_score`` 不变）；
- ``tier`` = 命中的最低（最可信）通道档位，**只做元数据/资格线/rerank 特征，绝不作 ORDER BY**；
- 池内候选补齐切片元数据（kind/path/行号/symbol）——一次 ``chunks_by_ids`` 批量读。

通道 → tier 口径（TASK-010 冻结，TASK-011/012 依赖，已记入任务卡"执行记录"）：

| 通道 | 名 | tier | 说明 |
|---|---|---|---|
| Exact-Explicit | ``exact`` | 0 | 精确证据（强种子） |
| Literal | ``literal`` | 0 | 连续子串命中（TASK-101 §A：配置键/CLI 名/常量名不被分词切碎） |
| Exact-Inferred | ``inferred`` | 1 | 召回种子，非精确证据（D-15） |
| BM25 | ``bm25`` | 1 | 词法 |
| Vector | ``vector`` | 2 | 语义 |
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from nova_core.retrieval.rrf import fuse_rankings
from nova_core.storage import Store
from nova_core.types import Candidate

__all__ = [
    "CHANNEL_BM25",
    "CHANNEL_EXACT",
    "CHANNEL_INFERRED",
    "CHANNEL_LITERAL",
    "CHANNEL_VECTOR",
    "DEFAULT_POOL_LIMIT",
    "KIND_CODE",
    "KIND_FALLBACK",
    "KIND_SPEC",
    "KIND_TEST",
    "REASON_EXPLICIT_PATH",
    "TIER_EXPLICIT",
    "TIER_SEED",
    "TIER_VECTOR",
    "classify_kind",
    "is_test_path",
    "make_candidate",
    "merge",
]

CHANNEL_EXACT = "exact"
CHANNEL_INFERRED = "inferred"
CHANNEL_BM25 = "bm25"
#: 字面量通道（TASK-101 §A）；tier 0 —— 子串命中是精确证据，与 Explicit 同级。
CHANNEL_LITERAL = "literal"
CHANNEL_VECTOR = "vector"

TIER_EXPLICIT = 0
TIER_SEED = 1
TIER_VECTOR = 2

#: RRF 池规模（Module/02 §4.3："输出 ~120 候选"）。
DEFAULT_POOL_LIMIT = 120

#: 路径词元命中候选的解释文本（rerank 的 "+Explicit symbol/path 命中" 特征依据）。
REASON_EXPLICIT_PATH = "explicit path"

KIND_CODE = "code"
KIND_TEST = "test"
KIND_SPEC = "spec"
KIND_FALLBACK = "fallback"

_TEST_DIR_RE = re.compile(r"(?:^|/)(?:tests?|testdata|fixtures)(?:/|$)")
_TEST_FILE_RE = re.compile(r"(?:^|/)(?:test_[^/]*|[^/]*_test)\.[A-Za-z0-9]+$")


def is_test_path(path: str) -> bool:
    """路径是否属于测试代码（tests/ 目录或 test_*/_test 命名）。

    ``Candidate.kind`` 的 test 判定与 TASK-011 rerank 的 "test fixture（非测试意图查询）−0.5"
    共用本判定；TASK-011 只读导入，不修改本模块（文件所有权在 TASK-010）。
    """
    return bool(_TEST_DIR_RE.search(path) or _TEST_FILE_RE.search(path))


def classify_kind(path: str | None, symbol_kind: str | None) -> str:
    """切片 → 候选 ``kind``（code/test/spec/fallback，与 CF-04 / ContextPack 对齐）。"""
    if symbol_kind == "spec_block":
        return KIND_SPEC
    if symbol_kind == "fallback_block":
        return KIND_FALLBACK
    if path and is_test_path(path):
        return KIND_TEST
    return KIND_CODE


def make_candidate(
    chunk_id: str,
    *,
    channel: str,
    rank: int,
    tier: int,
    reason: str | None = None,
) -> Candidate:
    """构造单通道候选（``rrf_score``/``score`` 由融合阶段填写）。"""
    reasons = [reason] if reason else []
    reasons.append(f"{channel} rank {rank}")
    return Candidate(
        chunk_id=chunk_id,
        kind=KIND_CODE,
        rrf_score=0.0,
        score=0.0,
        channel_ranks={channel: rank},
        tier=tier,
        reasons=reasons,
    )


def merge(
    channel_candidates: dict[str, Sequence[Candidate]],
    *,
    store: Store | None = None,
    pool_limit: int = DEFAULT_POOL_LIMIT,
    explicit_paths: Sequence[str] = (),
) -> list[Candidate]:
    """多通道候选 → RRF 去重合并后的候选池（降序）。

    - 传入的 ``Sequence[Candidate]`` 顺序即该通道 rank（1-based，先到先得）；
    - 合并后补齐切片元数据（``store`` 非空时）；库里已不存在的 chunk 直接丢弃
      （索引已变，候选不再是证据）；
    - ``explicit_paths``：查询显式点名的路径 → 命中候选升到 tier 0 并标注原因
      （**V1 口径**：Store 无"按路径列切片"的读 API，路径词元只对**已被召回**的同路径候选
      生效，不做文件枚举；详见任务卡"执行记录/未决问题"）。
    """
    rankings = {
        channel: _channel_ranks(candidates, channel)
        for channel, candidates in channel_candidates.items()
        if candidates
    }
    if not rankings:
        return []

    by_chunk: dict[str, list[Candidate]] = {}
    for candidates in channel_candidates.values():
        for candidate in candidates:
            by_chunk.setdefault(candidate.chunk_id, []).append(candidate)

    entries = fuse_rankings(rankings)[:pool_limit]
    merged: list[Candidate] = []
    for entry in entries:
        contributing = by_chunk.get(entry.chunk_id, [])
        reasons = _merge_reasons(contributing)
        merged.append(
            Candidate(
                chunk_id=entry.chunk_id,
                kind=KIND_CODE,
                rrf_score=entry.rrf_score,
                score=entry.rrf_score,
                channel_ranks=dict(entry.channel_ranks),
                tier=min((c.tier for c in contributing), default=TIER_VECTOR),
                reasons=reasons,
                graph_depth=0,
            )
        )

    if store is not None:
        merged = _enrich(merged, store)
    _apply_explicit_paths(merged, explicit_paths)
    return merged


def _channel_ranks(candidates: Sequence[Candidate], channel: str) -> dict[str, int]:
    """候选序列 → ``chunk_id → rank``（以候选自报的通道排名为准，缺省用位置）。

    各 ``recall_*`` 已按相关性排序并写入 ``channel_ranks``；以自报排名为准可避免调用方
    重新排序时静默改变 RRF 结果（例如通道内部做过截断）。重复 chunk_id 保留首次出现。
    """
    ranks: dict[str, int] = {}
    for position, candidate in enumerate(candidates, start=1):
        rank = candidate.channel_ranks.get(channel, position)
        ranks.setdefault(candidate.chunk_id, rank)
    return ranks


def _merge_reasons(candidates: Iterable[Candidate]) -> list[str]:
    """合并各通道原因文本（去重、保持通道顺序）。"""
    merged: list[str] = []
    for candidate in candidates:
        for reason in candidate.reasons:
            if reason not in merged:
                merged.append(reason)
    return merged


def _enrich(candidates: list[Candidate], store: Store) -> list[Candidate]:
    """补齐 kind/symbol_fqn/path/行号；缺失切片（索引已变）丢弃。"""
    chunks = {chunk.id: chunk for chunk in store.chunks_by_ids([c.chunk_id for c in candidates])}
    enriched: list[Candidate] = []
    for candidate in candidates:
        chunk = chunks.get(candidate.chunk_id)
        if chunk is None:
            continue
        candidate.kind = classify_kind(chunk.file_path, chunk.symbol_kind)
        candidate.symbol_fqn = chunk.symbol_fqn
        candidate.path = chunk.file_path
        candidate.start_line = chunk.start_line
        candidate.end_line = chunk.end_line
        enriched.append(candidate)
    return enriched


def _apply_explicit_paths(candidates: list[Candidate], explicit_paths: Sequence[str]) -> None:
    if not explicit_paths:
        return
    wanted = tuple(explicit_paths)
    for candidate in candidates:
        path = candidate.path
        if not path:
            continue
        if any(path == token or path.endswith("/" + token) for token in wanted):
            candidate.tier = TIER_EXPLICIT
            if REASON_EXPLICIT_PATH not in candidate.reasons:
                candidate.reasons.append(REASON_EXPLICIT_PATH)
