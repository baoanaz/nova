"""``ContextPack`` → ``meta`` 字典的**唯一**转换点（TASK-032 §交付物）。

为什么单独成文件：``search`` 与 ``ask`` 两个端点共用同一份 ``meta``，而这份 ``meta`` 是
**TASK-040（Rust client）的输入契约**；转换逻辑集中在 1 个函数里，才谈得上"写出后不得随意改"。

``meta`` 字段集（本卡冻结，TASK-040 依赖；字段只增不改，改字段等于改客户端契约）::

    {
      projectId, query, mode, checkpointId,
      answerable, confidence,
      channelsUsed: [str], degraded: bool, degradedReason: str|null,
      candidateCount: int,
      freshness: {indexedAt: int|null, staleFiles: [str], indexingFiles: [str]},
      budget: {usedTokens: int, hardCap: int, truncated: bool, omittedCount: int},
      evidenceCount: int, docsCount: int, flowsCount: int,
      missingEvidence: [{code, message, symbol|null}],
      pack: object|null            # includePack=true 时，CF-03 的完整 JSON
    }

说明（两处口径）：
- ``mode`` 取 ``pack.mode``（= 实际组装用的模式）：``ask`` 在 Phase 2 走的是 Fast 组装
  （Deep 的 LLM 与二轮检索属 Phase 3），因此 ``mode="fast"`` 而 ``status="degraded"``——
  这是如实描述"实际发生了什么"，不用 ``"deep"`` 掩盖降级；
- ``checkpointId`` 只做**透传记录**（值不参与检索，卡内明确）；本卡先记录下来供 client 对账，
  TASK-033 再做 checkpoint 登记与校验。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from nova_core.contextpack import to_json
from nova_core.types import ContextPack, EvidenceItem

__all__ = ["DECISION_SUMMARY_LIMIT", "evidence_summary", "meta_field_names", "pack_meta"]

#: ``ask.evidenceSummary`` 的条数上限（CF-05 之外的扩展字段；client 用于展示证据概览）。
DECISION_SUMMARY_LIMIT = 10

#: ``meta`` 的字段名集合（测试用它守住"字段集冻结"）。
_META_FIELDS = (
    "projectId",
    "query",
    "mode",
    "checkpointId",
    "answerable",
    "confidence",
    "channelsUsed",
    "degraded",
    "degradedReason",
    "candidateCount",
    "freshness",
    "budget",
    "evidenceCount",
    "docsCount",
    "flowsCount",
    "missingEvidence",
    "pack",
)


def meta_field_names() -> tuple[str, ...]:
    """``meta`` 的字段名（冻结契约的自证入口；测试直接断言它与实现一致）。"""
    return _META_FIELDS


def pack_meta(
    pack: ContextPack,
    *,
    project_id: str,
    channels: Sequence[str] | None = None,
    degraded: bool = False,
    reason: str | None = None,
    candidate_count: int | None = None,
    checkpoint_id: str | None = None,
    include_pack: bool = False,
) -> dict[str, Any]:
    """``ContextPack`` + 检索 trace → ``meta``（所有字段转换只在这里发生）。"""
    budget = pack.budget
    return {
        "projectId": project_id,
        "query": pack.query,
        "mode": pack.mode,
        "checkpointId": checkpoint_id,
        "answerable": pack.answerable,
        "confidence": pack.confidence,
        "channelsUsed": list(channels or ()),
        "degraded": bool(degraded),
        "degradedReason": reason,
        "candidateCount": (
            candidate_count
            if candidate_count is not None
            else len(pack.evidence) + len(pack.docs)
        ),
        "freshness": {
            "indexedAt": pack.freshness.indexed_at,
            "staleFiles": list(pack.freshness.stale_files),
            "indexingFiles": list(pack.freshness.indexing_files),
        },
        "budget": {
            "usedTokens": budget.used_tokens if budget else 0,
            "hardCap": budget.hard_cap if budget else 0,
            "truncated": budget.truncated if budget else False,
            "omittedCount": budget.omitted_count if budget else 0,
        },
        "evidenceCount": len(pack.evidence),
        "docsCount": len(pack.docs),
        "flowsCount": len(pack.flows),
        "missingEvidence": [
            {"code": item.code, "message": item.message, "symbol": item.symbol}
            for item in pack.missing_evidence
        ],
        "pack": to_json(pack) if include_pack else None,
    }


def evidence_summary(
    pack: ContextPack, *, limit: int = DECISION_SUMMARY_LIMIT
) -> list[dict[str, Any]]:
    """``ask`` 的证据概览（``[{id, type, path, lines, tier, score}]``，按装填顺序取前 N 条）。

    ``evidence`` 与 ``docs`` 共用 E 编号空间（D-21），编号即装填顺序，因此合并后按编号排序
    才是真正的"最相关在前"。
    """
    items: list[EvidenceItem] = [*pack.evidence, *pack.docs]
    items.sort(key=_evidence_order)
    return [
        {
            "id": item.id,
            "type": item.type,
            "path": item.path,
            "lines": list(item.lines) if item.lines is not None else None,
            "tier": item.evidence_tier,
            "score": item.score,
        }
        for item in items[:limit]
    ]


def _evidence_order(item: EvidenceItem) -> int:
    return int(item.id[1:]) if item.id[1:].isdigit() else 0
