"""RRF 融合（TASK-010）：纯函数、无 I/O、无状态。

设计依据：``docs/design/Module/02-检索策略.md`` §4.3（D-16 纯 RRF 立场）：

    score(c) = Σ_通道 1 / (60 + rank_通道(c))

- ``RRF_K = 60`` 冻结（contracts.md §2 不变式 6）；**融合层不加通道权重**，通道差异化
  全部交给 rerank 层（TASK-011）；
- rank 从 **1** 开始；同一通道内的重复 chunk_id 只计首次出现（调用方通常已去重）；
- 排序是确定性的：同分时先看命中通道数（共识优先），再看最佳单通道排名，最后按
  chunk_id 字典序兜底（跨进程可复现）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

__all__ = ["RRF_K", "RrfEntry", "fuse_rankings", "reciprocal_rank", "rrf_score"]

#: RRF 常数（D-16；contracts.md §2 不变式 6 要求不得改动）。
RRF_K = 60


def reciprocal_rank(rank: int) -> float:
    """单通道第 ``rank`` 名（1-based）的 RRF 贡献 ``1/(K+rank)``。"""
    if rank < 1:
        raise ValueError(f"rank 必须 ≥ 1（1-based），收到 {rank!r}")
    return 1.0 / (RRF_K + rank)


def rrf_score(ranks: Sequence[int]) -> float:
    """多通道排名 → RRF 融合分（纯加法，无权重、无归一化）。"""
    return sum(reciprocal_rank(rank) for rank in ranks)


@dataclass(frozen=True, slots=True)
class RrfEntry:
    """融合后的一个候选：RRF 分 + 各通道排名（``{"bm25": 3, "vector": 7}``）。"""

    chunk_id: str
    rrf_score: float
    channel_ranks: dict[str, int]

    @property
    def channel_count(self) -> int:
        """命中通道数（rerank 的"3 通道共识"特征依据）。"""
        return len(self.channel_ranks)


def fuse_rankings(
    channel_rankings: Mapping[str, Sequence[str] | Mapping[str, int]],
) -> list[RrfEntry]:
    """多通道排名 → RRF 融合条目（按 RRF 分降序，确定性 tie-break）。

    ``channel_rankings``：通道名 → 该通道的排名信息，两种形态均可：

    - ``Sequence[str]``：有序 chunk_id 序列，位置即 rank（1-based）；
    - ``Mapping[str, int]``：``chunk_id → rank``（rank 由产生候选的通道给定，允许不连续）。

    同一通道内重复 chunk_id 只保留首次出现的 rank；空通道忽略。
    """
    ranks_by_chunk: dict[str, dict[str, int]] = {}
    for channel, ranked in channel_rankings.items():
        if isinstance(ranked, Mapping):
            pairs = [(chunk_id, rank) for chunk_id, rank in ranked.items()]
        else:
            pairs = [(chunk_id, position) for position, chunk_id in enumerate(ranked, start=1)]
        for chunk_id, rank in pairs:
            per_chunk = ranks_by_chunk.setdefault(chunk_id, {})
            per_chunk.setdefault(channel, int(rank))

    entries = [
        RrfEntry(
            chunk_id=chunk_id,
            rrf_score=rrf_score(list(per_chunk.values())),
            channel_ranks=per_chunk,
        )
        for chunk_id, per_chunk in ranks_by_chunk.items()
    ]
    entries.sort(key=_entry_sort_key)
    return entries


def _entry_sort_key(entry: RrfEntry) -> tuple[float, int, int, str]:
    """同分确定性排序：分降 → 通道数降（共识优先）→ 最佳排名升 → chunk_id。"""
    return (
        -entry.rrf_score,
        -entry.channel_count,
        min(entry.channel_ranks.values()),
        entry.chunk_id,
    )
