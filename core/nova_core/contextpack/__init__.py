"""ContextPack 组装与渲染（TASK-012 / CF-03 / D-21）。

对外入口：
- ``assemble(store, query, candidates, ...)``：候选（已 rerank）+ flows + freshness
  → ContextPack；
- ``render_markdown(pack)`` / ``render_evidence_for_prompt(pack)``：双层合同共用 formatter；
- ``to_json(pack)``：CF-03 JSON（供 05-MCP / Phase 2 service 直接返回）；
- ``BudgetConfig`` / ``budget_for(mode)``：Fast 10K / Deep 12K 预算配置（D-23）；
- ``BudgetConfig`` / ``budget_for(mode)``：Fast 10K / Deep 12K 预算配置（D-23）；
  ``CONTEXT_SCORE_RATIO``（默认 0.50）= TASK-095 §A 的相对分数阈值，可用环境变量
  ``NOVA_CONTEXT_SCORE_RATIO`` 覆盖（**不暴露为 MCP 工具参数**，CF-06 冻结）；
- ``collect_index_signals(store, candidates)``：G4/stale 与 unresolved 信号收集；
- ``estimate_tokens`` / ``estimate_render_tokens``：token 估算（TASK-096 §A：预算账=渲染账）。
"""

from __future__ import annotations

from nova_core.contextpack.assembly import (
    ADJACENT_GAP_LINES,
    CONTEXT_SCORE_RATIO,
    DEEP_BUDGET,
    FAST_BUDGET,
    MODE_DEEP,
    MODE_FAST,
    SCORE_RATIO_ENV,
    BudgetConfig,
    IndexSignals,
    assemble,
    budget_for,
    collect_index_signals,
    elision_note,
    estimate_render_tokens,
    estimate_tokens,
    evidence_markdown_lines,
    has_elision_note,
    numbered_lines,
    to_json,
)
from nova_core.contextpack.render import render_evidence_for_prompt, render_markdown

__all__ = [
    "ADJACENT_GAP_LINES",
    "CONTEXT_SCORE_RATIO",
    "DEEP_BUDGET",
    "FAST_BUDGET",
    "MODE_DEEP",
    "MODE_FAST",
    "SCORE_RATIO_ENV",
    "BudgetConfig",
    "IndexSignals",
    "assemble",
    "budget_for",
    "collect_index_signals",
    "elision_note",
    "estimate_render_tokens",
    "estimate_tokens",
    "evidence_markdown_lines",
    "has_elision_note",
    "numbered_lines",
    "render_evidence_for_prompt",
    "render_markdown",
    "to_json",
]
