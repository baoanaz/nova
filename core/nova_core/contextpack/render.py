"""ContextPack 渲染（TASK-012 §B）：对外 Markdown（Module/03 §6，D-21 双层合同）。

节顺序固定：``## Relevant Context`` → Code / Flow / Docs / Missing Evidence /
Suggested Next Queries / Meta。TASK-087 在 ``Missing Evidence`` 与 ``Meta`` 之间补了
``Suggested Next Queries``（D-24 的"有用的失败"：先给证据、再给缺口、最后给下一步该问什么），
``next_queries`` 为空时**整节不渲染**（空节白占 token）。

TASK-095 §B：``### Code`` 节**内部**按置信度分 ``#### Core`` / ``#### Related`` / ``#### Tests``
（四级标题，**不新增顶层节**；Flow / Docs 节不分组）。分组是**确定性**的（无 LLM）：

- ``Core``：``score ≥ top1 × 0.70`` 且 ``type != test``；
- ``Related``：``score ≥ top1 × 0.50`` 且 ``type != test``（即闸门内但非 Core）；
- ``Tests``：``type == test``（**无论分数**）——直接解决"测试文件因字面匹配排第一"，
  **不动 rerank 分**，只在渲染层归类；
- 空组不渲染；``[E*]`` 的编号与顺序**不因分组而重排**（编号 = 装填顺序，D-21）。

引用格式 ``[E*]`` / ``[F*]`` 与合同一致（04 citation 的锚点）；代码带行号（agent 可直接对齐
Edit）；stale 文档带 ``⚠`` 行；budget/confidence/index 状态入 ``Meta``。

``render_evidence_for_prompt`` 是同一 formatter 的子函数（只有 Code/Docs 分节），
供 Module/04 的 prompt 组装在 Phase 3 直接复用——两处绝不各写一套渲染逻辑。

省略标注（``... （省略 N 行）``）由组装层**就地**写在 ``item.content`` 里（TASK-017 / R12：
标注位置与行号区间一致）；本层只对外部构造、未就地标注的 pack 兜底追加。
"""

from __future__ import annotations

import time
from collections.abc import Sequence

from nova_core.contextpack.assembly import elision_note, estimate_tokens, has_elision_note
from nova_core.contextpack.reason import agent_reason
from nova_core.types import ContextPack, EvidenceItem, Freshness

__all__ = ["evidence_group", "render_evidence_for_prompt", "render_markdown"]

#: TASK-095 §B-2：``#### Core`` 的门槛（``score ≥ top1 × 本值`` 且非 test）。
#: 与 `assembly.BudgetConfig.score_ratio`（默认 0.50，装填闸门）**同值域但用途不同**：
#: 闸门决定"装不装"，本常量只决定"装进来的归哪一组"。两者都是 0.50/0.70 的实测取值，
#: 测量过程见 TASK-095 执行记录；**不要**用 golden smoke 集去优化它们（R29/R30 冻结）。
CORE_SCORE_RATIO = 0.70

#: Code 节内部分组的组名（按渲染顺序；与 §B-1 的示例逐字一致）。
#:
#: TASK-108：这三个值同时作为”证据分组“的**对外取值**（历史页要展示工具返回的分组结构，
#: 而不再是只展示路径）。文案与渲染标题保持一致，但**不带 Markdown 的 ``####`` 前缀**
#: （历史页存的是结构化字段，不是 Markdown）。
GROUP_CORE = "Core"
GROUP_RELATED = "Related"
GROUP_TESTS = "Tests"
GROUP_DOCS = "Docs"

_CORE_HEADING = f"#### {GROUP_CORE}"
_RELATED_HEADING = f"#### {GROUP_RELATED}"
_TESTS_HEADING = f"#### {GROUP_TESTS}"


def evidence_group(
    item: EvidenceItem, *, top1: float
) -> str:
    """单条证据的**分组名**（TASK-108：供历史页与渲染共用同一判定）。

    规则与 :func:`_group_evidence` 逐字一致（Core=非 test 且 score≥top1×0.70；
    Related=其余非 test；Tests=test；spec 归 Docs）——两处必须同源，否则历史页显示的
    分组会与 Agent 实际看到的 Markdown 分组不一致。
    """
    if item.type == "spec":
        return GROUP_DOCS
    if item.type == "test":
        return GROUP_TESTS
    if top1 > 0.0 and item.score >= top1 * CORE_SCORE_RATIO:
        return GROUP_CORE
    return GROUP_RELATED


#: 面向 Agent 的用法提示：实测 Agent 拿到 search 结果后 85% 的 read 是重读已返回的文件
#: （不确定片段是否完整），同一段代码进两次上下文。低置信度时不写（那时该看 Retry Tips）。
CORE_USAGE_NOTE = (
    "以下代码为仓库原文、行号准确，可直接引用作答，无需再 read 同一段；"
    "只列位置、没有正文的条目需要时再 read。"
)

#: 面向 Agent 的正文预算：Core 总是给正文，Related 按排名给到累计约这么多 token 为止。
#: 全给正文时单次 search 约 6k token（Agent 仍会重读）；只给 Core 又会丢掉排在 Core
#: 门槛之下的答案（场景评测 38/45 → 31/45）。
AGENT_BODY_TOKENS = 3_000


def render_markdown(pack: ContextPack, *, now: int | None = None) -> str:
    """完整 ContextPack → Markdown（Fast 模式 MCP 直接返回的形态）。

    面向 Agent 的形态“少而深”：Core 给正文，Related / Tests 只给位置行（不带代码）。
    ask 的 prompt 走 :func:`render_evidence_for_prompt`，仍是全部正文。
    """
    sections: list[str] = ["## Relevant Context"]
    if pack.evidence and pack.confidence != "low":
        sections.append(CORE_USAGE_NOTE)
    sections.extend(_code_section(pack, locate_only=True))
    sections.extend(_flow_section(pack))
    sections.extend(_docs_section(pack))
    sections.extend(_missing_section(pack))
    sections.extend(_next_queries_section(pack))
    sections.extend(_retry_tips_section(pack))
    sections.append(_meta_section(pack, now=now))
    return "\n".join(sections)


def render_evidence_for_prompt(pack: ContextPack) -> str:
    """04 prompt 的 evidence 分节（复用同一 formatter，Phase 3 直接用）。"""
    sections: list[str] = []
    sections.extend(_code_section(pack, heading="### Code"))
    sections.extend(_docs_section(pack))
    return "\n".join(sections).strip()


# --------------------------------------------------------------------------- 分节


def _code_section(
    pack: ContextPack, *, heading: str = "### Code", locate_only: bool = False
) -> list[str]:
    """``### Code`` 节。

    TASK-095 §B：节内按置信度分 ``#### Core`` / ``#### Related`` / ``#### Tests``。分组
    只影响**标题与分组归属**，不改任何一条证据的编号——三个组的拼接顺序
    （Core → Related → Tests）保证 ``[E*]`` 仍按装填顺序递增（编号 = 装填顺序，D-21）。
    空组不渲染（全是 Core 时不出现 ``#### Related`` / ``#### Tests``）。

    ``locate_only``：Core 全部给正文；Related 按装填顺序给正文直到累计
    :data:`AGENT_BODY_TOKENS`，其余与 Tests 只渲染位置行（``[E*] 符号 — 路径:行``）。
    """
    if not pack.evidence:
        return []
    lines = [heading]
    body_tokens = 0
    for group_heading, items in _group_evidence(pack.evidence):
        if not items:
            continue  # 空组不渲染（空标题白占 token）
        lines.append(group_heading)
        for item in items:
            body = _evidence_lines(item)
            cost = estimate_tokens("\n".join(body))
            show = (
                not locate_only
                or group_heading == _CORE_HEADING
                or (group_heading == _RELATED_HEADING and body_tokens + cost <= AGENT_BODY_TOKENS)
            )
            if show:
                lines.extend(body)
                body_tokens += cost
            else:
                lines.append(_evidence_header(item))
    return lines


def _group_evidence(
    evidence: Sequence[EvidenceItem],
) -> list[tuple[str, list[EvidenceItem]]]:
    """按 §B-2 的确定性规则分组（顺序：Core → Related → Tests）。

    - ``Core``：``score ≥ top1 × 0.70`` **且** ``type != test``；
    - ``Related``：``score ≥ top1 × 0.50`` **且** ``type != test``；
    - ``Tests``：``type == test``，无论分数（测试因字面匹配拿最高分时也不与代码混排；
      **不动 rerank 分**，只在渲染层归类）。

    ``top1`` = **包内证据的最高分**（含 test，即 ``max(item.score)``）——与 §A 装填闸门的参考分
    同源：闸门用“池内非 spec 最高分”，而该候选必然是包内第一条证据，故两者一般相等；
    包内没有非 spec 证据（纯文档包）时本层不渲染 Code 节。

    为什么不把 test 排除在 ``top1`` 之外：卡内 §B-1 的示例正是“测试拿 100%、
    真正的答案（``Runtime``）拿 72% 故入 Core、59% 的邻居入 Related”；若以非 test 最高分
    为参考，Related 会被抬到几乎为空（实测把 ``Related`` 整组消掉）。

    两个边界（都按"不丢数据、不造新组"处理）：

    - 包内全是 test → 全部归 ``Tests``；
    - 低于 ``top1×0.50`` 却仍在包里（保底块 **不受 §A 闸门约束**，以及手工构造的 pack）
      → 归 ``Related``。它们确实进了包，不渲染才是真的丢数据；
      不另开第四个组（契约 / §B-2 口径不变）。
    """
    top1 = max((item.score for item in evidence), default=0.0)
    buckets: dict[str, list[EvidenceItem]] = {
        GROUP_CORE: [],
        GROUP_RELATED: [],
        GROUP_TESTS: [],
    }
    for item in evidence:
        buckets[evidence_group(item, top1=top1)].append(item)
    return [
        (_CORE_HEADING, buckets[GROUP_CORE]),
        (_RELATED_HEADING, buckets[GROUP_RELATED]),
        (_TESTS_HEADING, buckets[GROUP_TESTS]),
    ]


def _docs_section(pack: ContextPack) -> list[str]:
    if not pack.docs:
        return []
    lines = ["### Docs"]
    for item in pack.docs:
        lines.extend(_evidence_lines(item, is_doc=True))
    return lines


def _flow_section(pack: ContextPack) -> list[str]:
    if not pack.flows:
        return []
    lines = ["### Flow"]
    for flow in pack.flows:
        chain = " → ".join(node.symbol for node in flow.nodes)
        suffix = "（已截断）" if flow.truncated else ""
        lines.append(f"[{flow.id}] {chain}{suffix}")
    return lines


#: 只给维护者看的索引诊断：与当次查询无关（“全仓 285 个符号引用无法解析”“候选池裁掉 129 个”），
#: 每次都占 Agent 上下文，还会诱导它去追无意义的线索（实测建议查 ``__name__`` 的调用方）。
#: 仍保留在 ``pack.missing_evidence`` / meta 里，只是不进面向 Agent 的 Markdown。
AGENT_HIDDEN_GAPS = frozenset({"unresolved_reference", "retrieval_truncated"})


def _missing_section(pack: ContextPack) -> list[str]:
    shown = [item for item in pack.missing_evidence if item.code not in AGENT_HIDDEN_GAPS]
    if not shown:
        return []
    lines = ["### Missing Evidence"]
    for item in shown:
        symbol = f" ({item.symbol})" if item.symbol else ""
        lines.append(f"- [{item.code}]{symbol} {item.message}")
    return lines


#: 低置信度时给 Agent 的重试提示（确定性文案，不调用任何模型）。
RETRY_TIPS = (
    "相关性较低：这次返回的内容可能不是你要找的，请不要直接当作答案。可以这样提高命中：",
    "1. 换成代码里真实会出现的词重新搜索：函数/类/文件名，或英文关键词（例如把“怎么挑选线程”换成 "
    "`getNextLoop`、`round robin event loop`）；",
    "2. 已知文件路径或行号时直接 read，不必再搜；",
    "3. 需要跨文件的综合解释、机制或比较时改用 ask_project。",
)


def _retry_tips_section(pack: ContextPack) -> list[str]:
    """``### Retry Tips``：只在 ``confidence == "low"`` 时出现（高/中置信度不占 token）。"""
    if pack.confidence != "low":
        return []
    return ["### Retry Tips", *RETRY_TIPS]


def _next_queries_section(pack: ContextPack) -> list[str]:
    """D-24 的自愈查询（``pack.next_queries``）；为空时整节不渲染。

    每行形如 ``- <query>``，**不加编号**：证据编号 ``[E*]`` / ``[F*]`` 是 citation 锚点，
    给建议查询编号会让 Agent 误把它们当成可引用的证据。节名与既有节同为英文（§A）。

    只在 :func:`render_markdown`（面向 Agent 的形态）里出现；
    :func:`render_evidence_for_prompt` 是给 LLM prompt 的 evidence 分节，**不含**本节。
    """
    if not pack.next_queries:
        return []
    return ["### Suggested Next Queries", *(f"- {query}" for query in pack.next_queries)]


def _evidence_header(item: EvidenceItem, *, is_doc: bool = False) -> str:
    if is_doc:
        doctype = f"（{item.doctype}）" if item.doctype else ""
        heading = f" > {item.heading_path}" if item.heading_path else ""
        return f"[{item.id}] {item.path}{heading}{doctype}"
    target = item.symbol or item.path
    span = ""
    if item.lines is not None:
        span = f":{item.lines[0]}-{item.lines[1]}"
    return f"[{item.id}] {target} — {item.path}{span}"


def _evidence_lines(item: EvidenceItem, *, is_doc: bool = False) -> list[str]:
    lines = [_evidence_header(item, is_doc=is_doc)]
    reason = agent_reason(item.reason)
    if reason:
        lines.append(f"     reason: {reason}")
    lines.extend(f"     {line}" for line in item.content.splitlines())
    if item.elided_lines > 0 and not has_elision_note(item.content):
        lines.append(f"     {elision_note(item.elided_lines)}")
    if item.stale_refs:
        lines.append(f"     ⚠ 引用了已删除符号 {', '.join(item.stale_refs)}，文档可能过时")
    return lines


def _meta_section(pack: ContextPack, *, now: int | None) -> str:
    budget = pack.budget
    used = budget.used_tokens if budget else 0
    cap = budget.hard_cap if budget else 0
    confidence = pack.confidence
    return (
        f"### Meta\n"
        f"confidence: {confidence} | index: {_index_status(pack.freshness, now)} | "
        f"budget: {_thousands(used)}/{_thousands(cap)}"
    )


def _index_status(freshness: Freshness, now: int | None) -> str:
    if freshness.indexing_files:
        return f"indexing ({len(freshness.indexing_files)} files)"
    if freshness.stale_files:
        return f"stale ({len(freshness.stale_files)} files)"
    if freshness.indexed_at is None:
        return "unknown"
    reference = int(time.time()) if now is None else now
    age = max(reference - freshness.indexed_at, 0)
    return f"fresh ({_age(age)} ago)"


def _age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60} min"
    return f"{seconds // 3600} h"


def _thousands(tokens: int) -> str:
    if tokens < 1000:
        return str(tokens)
    return f"{tokens / 1000:.1f}K"
