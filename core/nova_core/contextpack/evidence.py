"""请求内证据集合与用途分流（D-50）。

EvidenceSet 保留未按输出预算裁剪的排序候选；两个组装器直接从集合读取源码，
ask 不从 SearchPack 重建输入。仍复用 CF-03 ContextPack 和原装填规则，
后续可以分别优化选择器而不复制召回链路。

集合不持久化、不持有 Store，不代表不可变索引快照。引擎在同一次 Store 打开期间
完成发现和组装；Candidate 沿用 CF-04 可变类型，消费方不得原地修改集合中的候选。
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from nova_core.contextpack.assembly import (
    MODE_DEEP,
    MODE_FAST,
    BudgetConfig,
    IndexSignals,
    assemble,
)
from nova_core.storage import Store
from nova_core.types import Candidate, ContextPack, Flow, Freshness


@dataclass(frozen=True, slots=True)
class EvidenceSet:
    """共享证据发现的输出，保留完整候选、关系与有界补检来源。"""

    query: str
    candidates: tuple[Candidate, ...]
    flows: tuple[Flow, ...]
    freshness: Freshness
    signals: IndexSignals
    backfill: tuple[tuple[Candidate, str], ...] = ()
    repair_triggered: bool = False


def build_search_pack(
    store: Store, evidence: EvidenceSet, *, config: BudgetConfig
) -> ContextPack:
    """面向 Agent 的 Fast 证据包；不调用总结模型。"""
    active = replace(
        config, prefer_explicit=True, focus_windows=True,
        focus_symbols=tuple(c.symbol_fqn for c in evidence.candidates[:10] if c.symbol_fqn),
    )
    return _build_pack(store, evidence, mode=MODE_FAST, config=active)


def build_ask_evidence(
    store: Store, evidence: EvidenceSet, *, config: BudgetConfig
) -> ContextPack:
    """面向总结模型的 Deep 证据；与 search 返回包及预算独立。"""
    return _build_pack(store, evidence, mode=MODE_DEEP, config=config)


def _build_pack(
    store: Store, evidence: EvidenceSet, *, mode: str, config: BudgetConfig
) -> ContextPack:
    # CF-04 Candidate 含可变 reasons/channel_ranks；旧组装器的 C/C++ 代表选择
    # 会追加聚合说明。每次构建使用独立副本，不能污染共享集合或后续 Gap 判定。
    copies: dict[int, Candidate] = {}

    def isolated(candidate: Candidate) -> Candidate:
        key = id(candidate)
        if key not in copies:
            copies[key] = replace(
                candidate,
                reasons=list(candidate.reasons),
                channel_ranks=dict(candidate.channel_ranks),
            )
        return copies[key]

    return assemble(
        store,
        evidence.query,
        tuple(isolated(candidate) for candidate in evidence.candidates),
        flows=evidence.flows,
        freshness=evidence.freshness,
        mode=mode,
        config=config,
        signals=evidence.signals,
        backfill=tuple((isolated(candidate), reason) for candidate, reason in evidence.backfill),
        reserve_backfill=evidence.repair_triggered,
    )
