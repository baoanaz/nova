"""图扩展（TASK-011 §A）与调用链 flows（§C）。

设计依据：``docs/design/Module/02-检索策略.md`` §4.4-a（图是扩展器；D-18-a）：

```text
seeds = 候选池 top 20（按 score）
每 seed 取 symbol：
  ├─ calls 边 1-hop（callers + callees 双向）→ 目标 chunk 入池，tier=3，graph_depth=1
  └─ spec_references 双向：
       代码 seed → 引用它的 SpecBlock（设计意图）
       spec seed → 它提到的代码符号（实现位置）
防爆炸（硬性）：扩展总量 ≤ 30 块；单符号 callers > 200 时按入口点评分截断 top 20
```

外加 §C 的最小调用链 flows（供 ContextPack ``flows[]``）：top 3 种子沿 callees 方向取
≤3 节点链（无环、按行号稳定），编号 F1..Fn；无法构成 ≥2 节点链的种子不产出 flow
（"不存在的路径宁缺勿编"）。

实现口径（本卡冻结，记录于任务卡"执行记录"）：

- 扩展候选 ``rrf_score = 0.0``（它们从未进入 RRF 池）→ rerank 后其分数完全由特征决定
  （这正是 §4.5 "+ 与 top-1 种子图连通 1 跳 +0.5" 存在的理由）；
- 扩展来源写进 ``reasons``：``graph-expanded from <seed_chunk_id>``（rerank 判定"与 top-1
  种子 1 跳连通"的依据）与 ``synthesized edge``（provenance=synthesized，供 −0.2）；
- 已存在候选（同一 chunk_id）不重复扩展；扩展结果只返回**新增**部分，由调用方并入池。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from nova_core.retrieval.fusion import KIND_SPEC, classify_kind
from nova_core.storage import Store
from nova_core.types import Candidate, Flow, FlowNode

__all__ = [
    "GRAPH_REASON_PREFIX",
    "GRAPH_TIER",
    "REASON_REEXPORT",
    "SYNTHESIZED_REASON",
    "ExpansionLimits",
    "ExpansionResult",
    "build_flows",
    "expand",
]

#: 图扩展候选的 tier（D-17：可信度元数据 + 03 装填配额 ≤30%）。
GRAPH_TIER = 3

#: 扩展来源原因前缀（rerank 依赖该格式判定"与 top-1 种子 1 跳连通"）。
GRAPH_REASON_PREFIX = "graph-expanded from "

#: synthesized 边扩展标记（provenance='synthesized'，rerank −0.2）。
SYNTHESIZED_REASON = "synthesized edge"

#: 公开导出点标记（TASK-MCP-CHAIN）：该候选是 seed 符号的包入口重导出处。
REASON_REEXPORT = "re-export site"


@dataclass(frozen=True, slots=True)
class ExpansionLimits:
    """图扩展配额（Module/02 §4.4-a 的硬性防爆炸参数）。"""

    seeds: int = 20                      # 种子数：候选池 top 20
    max_expanded: int = 30               # 扩展总量硬上限
    caller_explosion_threshold: int = 200  # 单符号 callers 超过此值即截断
    caller_cap: int = 20                 # 爆炸时保留的 callers 数（入口点优先）
    flow_seeds: int = 3                  # 产出 flows 的种子数（top 3）
    flow_max_nodes: int = 3              # 单条链的最大节点数


@dataclass(frozen=True, slots=True)
class ExpansionResult:
    """扩展产物：新增候选（不含输入池）+ flows + 实际使用的种子。"""

    candidates: list[Candidate]
    flows: list[Flow]
    seed_chunk_ids: tuple[str, ...]


def _by_score(candidates: Iterable[Candidate]) -> list[Candidate]:
    """按 score 降序（同分用 rrf_score、chunk_id 兜底，保证可复现）。"""
    return sorted(candidates, key=lambda c: (-c.score, -c.rrf_score, c.chunk_id))


def _symbol_row(store: Store, fqn: str):
    """按 fqn 精确取符号行（优先带 chunk 的那条）。"""
    for row in store.exact_symbols(fqn, limit=None):
        if row.fqn == fqn and row.chunk_id is not None:
            return row
    return None


def _is_entry_point(store: Store, fqn: str, is_exported: bool) -> bool:
    """入口点 = 无内部调用者且被导出（GitNexus entry-point-scoring 思想）。"""
    if not is_exported:
        return False
    callers = [edge for edge in store.edges_for(fqn, kinds=["calls"]) if edge.target == fqn]
    return not callers


def _caller_sort_key(store: Store, fqn: str):
    """caller 截断排序：入口点优先 → 导出符号优先 → fqn 稳定。"""
    rows = store.exact_symbols(fqn, limit=None)
    exported = any(row.fqn == fqn and row.is_exported for row in rows)
    return (0 if _is_entry_point(store, fqn, exported) else 1, 0 if exported else 1, fqn)


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _callee_rank(store: Store, target: str) -> int:
    """callee 的“实现细节”分档（0 最优先）：

    - 0：**同仓私有实现函数**（`_name` 且非 dunder）——调用链题问的正是这些
      （实测 ``_make_tools_to_model_edge`` / ``_chain_model_call_handlers``）；
    - 1：同仓其它函数/类；
    - 2：落不到本仓切片的外部符号（``isinstance`` / ``len`` / ``graph.add_node``）。

    为什么不能直接用“执行顺序”（边行号）排序：实测 ``create_agent`` 的 90 条 callee
    里，``isinstance`` / ``graph.add_node`` 这类外部调用占了前 45 条，而真正的
    边构造函数 ``_make_tools_to_model_edge`` 排在第 75 位——图扩展的 30 条配额被
    外部调用吃光，调用链题（追问 model↔tools 边如何路由）全部落空。
    """
    rows = store.exact_symbols(target, limit=None)
    local = any(row.fqn == target and row.chunk_id is not None for row in rows)
    if not local:
        return 2
    name = target.replace("::", ".").rsplit(".", 1)[-1]
    return 0 if (name.startswith("_") and not _is_dunder(name)) else 1


def _calls_neighbours(store: Store, fqn: str, kind: str, limits: ExpansionLimits):
    """``kind='callers'``：指向本符号的 calls 入边；``kind='callees'``：本符号的出边。

    返回 ``(neighbour_fqn, provenance)`` 列表（callers 爆炸时按入口点评分截断）。
    """
    edges = store.edges_for(fqn, kinds=["calls"])
    if kind == "callers":
        selected = [edge for edge in edges if edge.target == fqn]
        if len(selected) > limits.caller_explosion_threshold:
            selected.sort(key=lambda edge: _caller_sort_key(store, edge.source))
            selected = selected[: limits.caller_cap]
        return [(edge.source, edge.provenance) for edge in selected]
    selected = [edge for edge in edges if edge.source == fqn]
    # TASK-MCP-CHAIN：callee 按“实现细节优先”排（见 :func:`_callee_rank`），不再按执行顺序。
    # 行号仍作为同档内的稳定次序（同一函数体内的声明顺序）。
    selected.sort(
        key=lambda edge: (
            _callee_rank(store, edge.target),
            edge.line if edge.line is not None else 0,
            edge.target,
        )
    )
    return [(edge.target, edge.provenance) for edge in selected]


def _candidate_from_chunk(store: Store, chunk, *, seed: Candidate, provenance: str) -> Candidate:
    """切 → 扩展候选（tier=3、graph_depth=1、来源写进 reasons）。"""
    reasons = [f"{GRAPH_REASON_PREFIX}{seed.chunk_id}"]
    if provenance == "synthesized":
        reasons.append(SYNTHESIZED_REASON)
    if provenance == REASON_REEXPORT:
        # 保留可读来源（G4 的判定依据），同时**仍带 GRAPH_REASON_PREFIX**：
        # 那条前缀是 rerank 的“与 top-1 种子结构相连 +0.5”特征来源。若去掉它，
        # 导出点候选的 ``score`` 会是 0.0（rrf=0 且无任何特征）——连补检候选池都进不去，
        # 等于白扩展（实测踩到）。用同一前缀是符合语义的：导出点确实与 seed 结构相连。
        reasons.append(REASON_REEXPORT)
    return Candidate(
        chunk_id=chunk.id,
        kind=classify_kind(chunk.file_path, chunk.symbol_kind),
        rrf_score=0.0,
        score=0.0,
        channel_ranks={},
        tier=GRAPH_TIER,
        reasons=reasons,
        symbol_fqn=chunk.symbol_fqn,
        path=chunk.file_path,
        start_line=chunk.start_line,
        end_line=chunk.end_line,
        graph_depth=1,
    )


def _expansion_candidate(
    store: Store,
    target_fqn: str,
    *,
    seed: Candidate,
    provenance: str,
) -> Candidate | None:
    """把邻居符号落成一个扩展候选（**总是能落到切片**——TASK-101 §B）。

    此前：符号无 ``chunk_id`` 就直接跳过（"不编造证据"）。实测代价过大：符号表里
    ``(module)`` 这类符号（以及类骨架之外的声明）没有 ``chunk_id``，而它们常是真实的 call 边端点
    ——实测 ``capability_definitions`` → ``tools/hmi/definitions.py`` 整条边被丢弃，
    于是"业务 Tool 定义在哪个文件"这类题在图扩展侧完全不可见。

    现在退回 ``Store.chunk_covering(file_path, start_line)``：符号表已经给了行号，
    取覆盖该行的切片即可。**仍然不编造**：拿不到行号或找不到覆盖切片时才返回 ``None``。
    """
    row = _symbol_row(store, target_fqn)
    if row is None:
        return None
    chunk = store.chunk_by_id(row.chunk_id) if row.chunk_id is not None else None
    if chunk is None and row.file_path is not None and row.start_line is not None:
        chunk = store.chunk_covering(row.file_path, row.start_line)
    if chunk is None:
        return None
    return _candidate_from_chunk(store, chunk, seed=seed, provenance=provenance)


def expand(
    store: Store,
    candidates: Sequence[Candidate],
    *,
    limits: ExpansionLimits | None = None,
) -> ExpansionResult:
    """RRF 候选池 → 图扩展候选（calls 1-hop + spec_references 双向）+ flows。"""
    active = limits or ExpansionLimits()
    pool = _by_score(candidates)
    seeds = pool[: active.seeds]
    known = {candidate.chunk_id for candidate in pool}
    expanded: list[Candidate] = []

    def _add(candidate: Candidate | None) -> bool:
        if candidate is None or candidate.chunk_id in known:
            return False
        if len(expanded) >= active.max_expanded:
            return False
        known.add(candidate.chunk_id)
        expanded.append(candidate)
        return True

    for seed in seeds:
        if len(expanded) >= active.max_expanded:
            break
        if seed.kind == KIND_SPEC:
            _expand_spec_seed(store, seed, _add)
            continue
        _expand_code_seed(store, seed, active, _add)
    return ExpansionResult(
        candidates=expanded,
        flows=build_flows(store, seeds, limits=active),
        seed_chunk_ids=tuple(seed.chunk_id for seed in seeds),
    )


def _expand_code_seed(store: Store, seed: Candidate, limits: ExpansionLimits, add) -> None:
    """代码 seed：calls 双向 1-hop + 引用它的 SpecBlock（设计意图）。"""
    fqn = seed.symbol_fqn
    if fqn:
        # 顺序（TASK-MCP-CHAIN）：**公开导出点 → callees → callers**。配额被吃完就没了，
        # 而三者的“确定性程度”与“对回答的不可替代性”都是递减的：
        #
        # 1. 导出点：名称精确匹配的 imports 边，最多 1-2 条，但它是“公开 API 在哪导出”
        #    这类问题的**唯一答案**（实测 Q1：换序前它被前面 30 条扩展挤掉，池里根本没有）；
        # 2. callees：调用链题问的实现细节（实测 ``_make_tools_to_model_edge`` 等）；
        # 3. callers：实测以**测试函数**为主（``create_agent`` 的 20 条 caller 里 17 条是
        #    ``tests/.../test_*.py``），对“实现链路”几乎无贡献。
        #
        # 反向可达性（“谁调用了我”）主要由 grep 与 ``### Flow`` 承担，不靠图扩展配额。
        _expand_reexport_sites(store, seed, add)
        for neighbour, provenance in (
            _calls_neighbours(store, fqn, "callees", limits)
            + _calls_neighbours(store, fqn, "callers", limits)
        ):
            add(_expansion_candidate(store, neighbour, seed=seed, provenance=provenance))

        if seed.path and seed.symbol_fqn and seed.start_line is not None:
            symbol_id = f"{seed.path}:{seed.symbol_fqn}:{seed.start_line}"
            for ref in store.spec_refs_for_symbols([symbol_id]):
                if ref.stale:
                    continue  # stale 引用不作为扩展证据（只进 03 的 MissingEvidence）
                chunk = store.chunk_by_id(ref.spec_block_id)
                if chunk is not None:
                    add(_candidate_from_chunk(store, chunk, seed=seed, provenance=ref.provenance))


def _expand_reexport_sites(store: Store, seed: Candidate, add) -> None:
    """补**公开导出点**（改 TASK-MCP-CHAIN）：导入该符号的 ``__init__.py`` 也是答案的一部分。

    为什么必须单独做（实测 langchain，2026-09-16）：“X 在哪里定义、又在哪里公开导出”
    是两个不同的位置，而包入口是 ``imports`` 边、**不属于 calls 图**，现有图扩展
    （只走 calls）结构上永远召不到它。实测：``create_agent`` 在
    ``langchain/agents/__init__.py`` 有一条 ``imports`` 边指向
    ``langchain.agents.factory.create_agent``，而该文件只靠向量 rank 8 得存、
    换个措辞就丢。

    收敛条件（三条同时成立，避免把 calls 图变成全图遍历）：

    1. 边类型 ``imports``（不是 calls/引用）；
    2. 边的**源文件**是 ``__init__.py``（包入口才是“公开导出点”）；
    3. 边的 target 的**尾部名字**与 seed 的符号名相同（同一符号，不是同名路径）。

    不额外限“同目录”：实测 langchain 的 ``agents/__init__.py`` 与
    ``agents/factory.py`` 同目录，但 Python 重导出跨目录也常见（如 ``__init__`` 里
    从子包导入），硬限同目录会漏掉后者；限定 ``__init__.py`` + 同名两项已经足够收敛。
    """
    fqn = seed.symbol_fqn
    if not fqn:
        return
    name = fqn.replace("::", ".").rsplit(".", 1)[-1]
    for source in store.reexport_sources(name):
        chunk = _chunk_for_file(store, source)
        if chunk is not None:
            add(_candidate_from_chunk(store, chunk, seed=seed, provenance=REASON_REEXPORT))


def _chunk_for_file(store: Store, path: str):
    """取文件的首个切片（``__init__.py`` 通常只有一个 module 级 fallback 块）。

    用 ``chunk_covering(path, 1)`` 而不是新加一个 Store 读 API：导入边不带行号，
    而包入口的 ``from ... import name`` 一定在第 1 行之下，取覆盖第 1 行的切片即可；
    这样不扩 ``Store`` 的公开面（它属 TASK-011 领地）。
    """
    return store.chunk_covering(path, 1)


def _expand_spec_seed(store: Store, seed: Candidate, add) -> None:
    """spec seed：它提到的代码符号（实现位置）。

    spec 块与同名 chunk 同 id（CF-01 裁定 2）；``symbol_id`` 就是目标 chunk id
    （``apply_file_change`` 只在符号有切片时写 ``chunk_id = symbol_id``）。
    """
    for ref in store.spec_refs_for_spec(seed.chunk_id):
        chunk = store.chunk_by_id(ref.symbol_id)
        if chunk is None:
            continue  # 符号在库中无切片（如仅声明）→ 不编造
        add(_candidate_from_chunk(store, chunk, seed=seed, provenance=ref.provenance))


def build_flows(
    store: Store,
    seeds: Sequence[Candidate],
    *,
    limits: ExpansionLimits | None = None,
) -> list[Flow]:
    """top N 种子沿 callees 方向的 ≤M 节点链（无环、按行号稳定）；F1..Fn。"""
    active = limits or ExpansionLimits()
    flows: list[Flow] = []
    for seed in seeds[: active.flow_seeds]:
        fqn = seed.symbol_fqn
        if not fqn or seed.kind == KIND_SPEC:
            continue
        nodes, truncated = _walk_callees(store, fqn, active.flow_max_nodes)
        if len(nodes) < 2:
            continue
        flows.append(Flow(id=f"F{len(flows) + 1}", nodes=tuple(nodes), truncated=truncated))
    return flows


def _walk_callees(store: Store, fqn: str, max_nodes: int) -> tuple[list[FlowNode], bool]:
    """从 fqn 沿 callees 走一条链；返回 (节点序列, 是否截断/遇环/分叉)。"""
    head = _symbol_row(store, fqn)
    if head is None or head.start_line is None or head.file_path is None:
        return [], False
    nodes = [FlowNode(symbol=head.fqn, path=head.file_path, line=head.start_line)]
    visited = {head.fqn}
    truncated = False
    current = head.fqn

    while len(nodes) < max_nodes:
        neighbours = _calls_neighbours(store, current, "callees", ExpansionLimits())
        usable: list[tuple[str, FlowNode]] = []
        for target, _provenance in neighbours:
            if target in visited:
                continue
            row = _symbol_row(store, target)
            if row is None or row.start_line is None or row.file_path is None:
                continue
            node = FlowNode(symbol=row.fqn, path=row.file_path, line=row.start_line)
            usable.append((target, node))
        if len(usable) != len(neighbours):
            # 有邻居成环或不可解析 → 链在此分叉/断裂，如实标注
            truncated = True
        if not usable:
            break
        chosen = usable[0][1]
        nodes.append(chosen)
        visited.add(chosen.symbol)
        current = chosen.symbol
        if len(usable) > 1:
            truncated = True  # 分叉：只取行号稳定排序的第一条

    if len(nodes) >= max_nodes:
        remaining = [
            target
            for target, _p in _calls_neighbours(store, current, "callees", ExpansionLimits())
            if target not in visited
        ]
        truncated = truncated or bool(remaining)
    return nodes, truncated
