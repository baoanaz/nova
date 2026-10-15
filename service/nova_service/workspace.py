"""D-54：服务端工作区证据合并，不推断跨项目调用关系。"""
from __future__ import annotations

from dataclasses import replace

from nova_core.contextpack import render_markdown
from nova_core.contextpack.assembly import estimate_tokens
from nova_core.types import Budget, ContextPack, Freshness, MissingEvidence

from nova_service.grounded import compact_evidence

WORKSPACE_CAP = 16_000


def combine_evidence(question, parts, gaps=()):
    """parts = (controlled label, projectId, pack)；调用方先完成授权。"""
    merged = ContextPack(question, "deep", False, "low", Freshness())
    origins = {}
    answerable_labels = set()
    used = omitted = 0
    truncated = False
    for label, project_id, pack in parts:
        if pack.answerable:
            answerable_labels.add(label)
        if pack.budget:
            omitted += pack.budget.omitted_count
            truncated |= pack.budget.truncated
        for destination, items in ((merged.evidence, pack.evidence), (merged.docs, pack.docs)):
            for item in items:
                identity = f"E{len(origins) + 1}"
                origins[identity] = {"projectId": project_id, "path": item.path, "project": label}
                destination.append(replace(
                    item, id=identity, path=f"{label}::{item.path}",
                    symbol=f"{label}::{item.symbol}" if item.symbol else None,
                ))
        for flow in pack.flows:
            identity = f"F{len(merged.flows) + 1}"
            origins[identity] = {"projectId": project_id, "project": label}
            merged.flows.append(replace(flow, id=identity, nodes=tuple(
                replace(node, path=f"{label}::{node.path}", symbol=f"{label}::{node.symbol}")
                for node in flow.nodes
            )))
        merged.missing_evidence.extend(
            replace(item, message=f"{label}: {item.message[:512]}",
                    symbol=f"{label}::{item.symbol}" if item.symbol else None)
            for item in pack.missing_evidence[:12]
        )
        if len(pack.freshness.stale_files) > 32 or len(pack.freshness.indexing_files) > 32:
            truncated = True
            omitted += max(0, len(pack.freshness.stale_files) - 32)
            omitted += max(0, len(pack.freshness.indexing_files) - 32)
        merged.freshness = Freshness(
            stale_files=merged.freshness.stale_files + tuple(
                f"{label}::{path}" for path in pack.freshness.stale_files[:32]),
            indexing_files=merged.freshness.indexing_files + tuple(
                f"{label}::{path}" for path in pack.freshness.indexing_files[:32]),
        )
    merged.missing_evidence.extend(MissingEvidence("workspace_gap", gap) for gap in gaps)
    merged.missing_evidence.append(MissingEvidence(
        "workspace_boundary", "按各项目当前存储的索引回答；同名不代表跨项目调用。",
    ))
    # Namespace/header additions must also fit; use both delivery and model-input estimates.
    while True:
        used = max(estimate_tokens(compact_evidence(merged)),
                   estimate_tokens(render_markdown(merged))) + 512
        if used <= WORKSPACE_CAP:
            break
        truncated = True
        omitted += 1
        if merged.freshness.stale_files or merged.freshness.indexing_files:
            merged.freshness = Freshness(
                stale_files=merged.freshness.stale_files[:len(merged.freshness.stale_files) // 2],
                indexing_files=merged.freshness.indexing_files[
                    :len(merged.freshness.indexing_files) // 2],
            )
        elif merged.flows:
            merged.flows.pop()
        elif merged.docs:
            merged.docs.pop()
        elif merged.evidence:
            merged.evidence.pop()
        elif len(merged.missing_evidence) > 1:
            merged.missing_evidence.pop(0)
        else:
            break
    ids = {item.id for item in (*merged.evidence, *merged.docs, *merged.flows)}
    origins = {key: value for key, value in origins.items() if key in ids}
    merged.answerable = any(
        origins[item.id]["project"] in answerable_labels
        for item in (*merged.evidence, *merged.docs)
    )
    merged.confidence = "medium" if merged.answerable else "low"
    merged.budget = Budget(used, WORKSPACE_CAP, truncated, omitted)
    return merged, origins

