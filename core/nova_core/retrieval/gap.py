"""Evidence-Gap 驱动的定向补检（TASK-109，Module/02 §4.7 的 G1/G2，D-19）。

设计依据：``docs/design/Module/02-检索策略.md`` §4.7（Gap 规则表）与 §4.8（快慢共享管线）。

**这一层解决的问题**（真实失败驱动，不是设计推演）：候选**已经正确索引、也已经在首轮候选池里**，
却没有进 ``ContextPack``。与"索引缺失""排序错误""阈值过严"都不同——它是一个**闭包缺口**：
包里已有锚点，而锚点在结构上必然决定的目标不在包里。

```text
首轮：召回 → RRF → 图扩展 → rerank → 装填      （pack 已产出）
                                    ↓
              Gap 检查（本模块，纯函数、无 I/O、无 LLM）
                                    ↓ 命中
              定向补检计划（chunk_id 列表 + 原因）
                                    ↓
              并入同一 rerank → 重组装（独立小预算，标记来源）
```

两条规则（本卡实现；G3/G4/G5 见"明确不做"）：

| 规则 | 缺口信号（确定性） | 补检动作 |
|---|---|---|
| **G1** 容器-成员断层 | 容器被查询点名且已在包内，池内成员全不在包内 | 补入池内成员 |
| **G2** 文档锚点闭包 | 包内 spec 块的引用目标在池内、不在包内 | 补入这些代码符号 |

两条规则的共同纪律（**本卡的核心设计约束**）：

1. **只在候选池内补**。补检目标必须是首轮已经召回过的候选（``chunk_id`` 在池内）。
   这条把"补召回"与"盲搜"分开：池外的候选说明首轮召回真没找到它，那属于检索通道的
   覆盖问题（召回配额/分词/embedding），不是本模块能诚实解决的——**凭空按符号名去库里
   捞一个没被召回的证据，等于绕开相关度判定**。
2. **确定性、无 LLM、无 I/O**。本模块是纯函数：符号成员查询由调用方（``engine``）注入，
   因此可以在毫秒内跑完，也便于单测。
3. **不预猜意图**（D-19）。触发信号全部来自"包里已有什么 + 池里还有什么"，
   不含任何"用户大概想问…"的分支。

本模块**不做**的事：不排序（并入 rerank 统一排，D-16 排序唯一性）、不装填
（装填规则在 ``contextpack``，含独立小预算与预算闸门）、不查库。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

__all__ = [
    "GAP_REASON_CALLEE",
    "GAP_REASON_PREFIX",
    "GAP_REASON_REEXPORT",
    "GAP_REASON_SPEC_REF",
    "GapLimits",
    "GapPlan",
    "SymbolMember",
    "plan_gaps",
]

#: 补检候选的 ``reasons`` 前缀（供 Agent 识别"这条是二轮补来的"，也是各规则分档的依据）。
GAP_REASON_PREFIX = "gap backfill: "

#: G1 的来源标注（写进 reason，进 ContextPack 供调用方判断可信度来源）。
GAP_REASON_CONTAINER = GAP_REASON_PREFIX + "同容器成员 "

#: G2 的来源标注。
GAP_REASON_SPEC_REF = GAP_REASON_PREFIX + "文档符号引用 "

#: G3 的来源标注（容器成员调用的实现函数）。
GAP_REASON_CALLEE = GAP_REASON_PREFIX + "容器成员的被调用方 "

#: G4 的来源标注（seed 符号的公开导出点）。
GAP_REASON_REEXPORT = GAP_REASON_PREFIX + "公开导出点 "


@dataclass(frozen=True, slots=True)
class SymbolMember:
    """容器成员的最小形状（``Store.symbols_in_container`` 的结构化子集）。

    用结构化 Protocol 而非直接依赖 ``storage.SymbolRow``：本模块是纯函数，
    单测可注入任意带这三个字段的对象，不必构造 Store。
    """

    fqn: str
    chunk_id: str | None = None


@dataclass(frozen=True, slots=True)
class GapLimits:
    """补检配额（防"补检变成全量重跑"，Module/02 §4.7 的"最多 1 次迭代"的量化）。"""

    #: 最多考察的容器锚点数（按 query 中出现顺序取前 N 个）。
    max_containers: int = 2
    #: 单容器最多补入的成员数（按首轮池序取，见 ``_named_containers``）。
    #:
    #: 取 24（大于本卡实测的最大容器成员数）而**不是**一个更紧的值：本模块的职责是
    #: “找出缺口”，**优先级排序在调用方**（``engine`` 按“已进包成员的被调用方优先”排）。
    #: 在这里提前截断会把真正的目标切掉——实测 ``CapabilityGateway`` 有 15 个在池成员，
    #: 取 12 时 ``_should_retry`` / ``_normalize`` 恰好落在第 13/14 位被截掉，
    #: 而按行数降序来的大方法反而入选。总额度由 ``max_total`` 与装填层的独立预算控制。
    members_per_container: int = 24
    #: 最多考察的 spec 锚点数（按包内装填顺序取前 N 个）。
    max_spec_anchors: int = 2
    #: 单 spec 锚点最多补入的引用目标数（按**首轮池序**取）。
    #:
    #: 取 4 而不是“文档引用了多少就补多少”：图扩展会把 spec 块引用的符号**全部**拉进
    #: 候选池，因此“引用目标都在池里”是必然成立的条件——本项才是真正的收敛阀。
    #: 不设阀的后果实测（cockpit-0033）：12 个引用目标全量入包，其中 8 个是
    #: ``AgentRegistry``/``HmiProvider`` 这类 3 行类骨架（score 0.200），把该题从
    #: “文档完整、代码缺失”变成“文档 + 一堆无关类名”——正是相对分数闸门要挡的东西，
    #: 补检不应把它放回来。
    refs_per_spec: int = 6
    #: G3（容器成员的被调用方）单容器最多补入数。
    #:
    #: 为什么需要 G3（实测 langchain，2026-09-16）：调用链题的目标常是**嵌套／模块级
    #: 实现函数**，它们不是被点名容器的符号成员，G1 的 ``members_of`` 结构上抓不到。
    #: 实测：问“``create_agent`` 的 model↔tools 边如何路由”，目标是
    #: ``_make_tools_to_model_edge``（工厂函数内的嵌套回调），而 ``create_agent`` 的
    #: callees 有 90 条（外部调用占掉大半）——即使图扩展把它拉进池，score 也是 0.0，
    #: 进不了包。G3 用“已进包成员 + 同名容器锚点”的 calls 出边把它补上。
    callees_per_container: int = 6
    #: 补检计划的总上限（跨规则、跨锚点）。
    max_total: int = 24


@dataclass(frozen=True, slots=True)
class GapPlan:
    """一次 Gap 检查的产物：命中规则 + 要补检的 chunk_id（按规则分组，供 engine 标注原因）。

    ``container_members`` / ``spec_refs`` 保留"哪个锚点带来了哪些 chunk"的分组，
    这样 reason 能写成 ``gap backfill: 同容器成员 CapabilityGateway`` 这类**可读来源**，
    而不是一条笼统的"补检"。
    """

    container_members: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    spec_refs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    callee_refs: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    reexport_sites: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def kinds(self) -> tuple[str, ...]:
        """命中的规则名（``G1`` / ``G2`` / ``G3`` / ``G4``，按固定顺序去重）。"""
        found: list[str] = []
        if self.container_members:
            found.append("G1")
        if self.spec_refs:
            found.append("G2")
        if self.callee_refs:
            found.append("G3")
        if self.reexport_sites:
            found.append("G4")
        return tuple(found)

    @property
    def chunk_ids(self) -> tuple[str, ...]:
        """全部待补检 chunk_id（跨规则去重，保持“G1 优先、声明顺序”的稳定序）。"""
        seen: list[str] = []
        for group in (
            *self.container_members.values(),
            *self.spec_refs.values(),
            *self.callee_refs.values(),
            *self.reexport_sites.values(),
        ):
            for chunk_id in group:
                if chunk_id not in seen:
                    seen.append(chunk_id)
        return tuple(seen)

    @property
    def triggered(self) -> bool:
        return bool(
            self.container_members
            or self.spec_refs
            or self.callee_refs
            or self.reexport_sites
        )

    def reason_for(self, chunk_id: str) -> str | None:
        """该 chunk 的补检来源标注（``gap backfill: …``）；不在计划内返回 ``None``。"""
        for anchor, group in self.container_members.items():
            if chunk_id in group:
                return f"{GAP_REASON_CONTAINER}{anchor}"
        for anchor, group in self.spec_refs.items():
            if chunk_id in group:
                return f"{GAP_REASON_SPEC_REF}{anchor}"
        for anchor, group in self.callee_refs.items():
            if chunk_id in group:
                return f"{GAP_REASON_CALLEE}{anchor}"
        for anchor, group in self.reexport_sites.items():
            if chunk_id in group:
                return f"{GAP_REASON_REEXPORT}{anchor}"
        return None


# --------------------------------------------------------------------------- 工具


def _unqualified(fqn: str) -> str:
    """符号规范化拼写：``A::b`` 与 ``A.b`` 视为同一符号（C++ / Python 抽取器差异）。"""
    return fqn.replace("::", ".")


def _member_of(fqn: str, container: str) -> bool:
    """``fqn`` 是否是 ``container`` 的成员（含更深层后代，``A.B.c`` 算 ``A`` 的成员）。"""
    return _unqualified(fqn).startswith(_unqualified(container) + ".")


def _named_containers(
    query_symbols: Iterable[str],
    packed_symbols: Iterable[str],
    packed_chunk_ids: Iterable[str],
    pool_chunk_ids: Sequence[str],
    members_of: Callable[[str], Sequence[SymbolMember]],
    limits: GapLimits,
) -> dict[str, tuple[str, ...]]:
    """G1：查询点名 + 已在包内 + 池内仍有未进包成员的容器 → 这些成员的 chunk_id。

    三个条件缺一不可（这是"不预猜意图"的落地）：

    - **查询点名**：容器名出现在查询的 Explicit/Inferred 符号里。否则任何包里出现的类
      都会触发，等于无条件扩张；
    - **已在包内**：该容器确实有符号进了包（类骨架/成员方法都算）——说明"这个容器"
      已经被判定为相关，补的是**它下面没进包的部分**，不是引入新主题；
    - **池内仍有未进包成员**：补检目标必须是首轮召回过的候选（见模块 docstring 纪律 1）。

    实测缺口（``cockpit-0035``）：``CapabilityGateway`` 类骨架 rank 5 进包（score 1.61），
    而 ``._reserve_idempotency``（rank 46）/``._normalize``（rank 49）/``._should_retry``
    都在池里却全未进包——用户问的恰恰是这些方法的行为，包内只剩一个类骨架。
    """
    packed_chunks = set(packed_chunk_ids)
    packed_set = {_unqualified(symbol) for symbol in packed_symbols}

    # 锚点 = 查询点名的名字 ``Q``，且包内存在 ``Q`` 的成员符号（``Q.x``）或 ``Q`` 自身。
    #
    # **不能用“包内符号的第一段”做锚点**（本卡实现过程中踩过的坑）：“第一段”对
    # C++ 命名空间类会取错——``leveldb::DBImpl::Get`` 的第一段是 ``leveldb``（命名空间），
    # 不是 ``leveldb::DBImpl``（容器），于是 C++ 仓库里的类永远匹配不上。
    # 前缀关系才是正确的判据（``_member_of``），它同时覆盖 Python 的 ``A.b``
    # 与 C++ 的 ``A::b``。
    anchors: list[str] = []
    for symbol in query_symbols:
        name = _unqualified(symbol)
        if name in anchors:
            continue
        if name in packed_set or any(
            packed.startswith(name + ".") for packed in packed_set
        ):
            anchors.append(symbol)

    plan: dict[str, tuple[str, ...]] = {}
    for anchor in anchors[: limits.max_containers]:
        # 成员按**首轮池内排名**取（不按声明顺序）：池序已经含 rerank 的相关度判定，
        # 直接复用它比另起一套排序更诚实，也避免了配额被尾部无关成员占满。
        # 实测（cockpit-0035）：按池序取前 12 个能覆盖 `_reserve_idempotency` /
        # `_should_retry` / `_normalize` 三个目标；按声明顺序则漏掉 `_should_retry`。
        wanted: dict[str, str] = {}
        for member in members_of(anchor):
            if not member.chunk_id or not member.fqn:
                continue
            normalized = _unqualified(member.fqn)
            if normalized == _unqualified(anchor) or normalized in packed_set:
                continue
            if member.chunk_id in packed_chunks:
                continue
            wanted.setdefault(member.chunk_id, member.fqn)
        if not wanted:
            continue
        ordered = [
            chunk_id for chunk_id in pool_chunk_ids if chunk_id in wanted
        ][: limits.members_per_container]
        if ordered:
            plan[anchor] = tuple(ordered)
    return plan


def _callee_closure(
    query_symbols: Iterable[str],
    packed_symbols: Iterable[str],
    packed_chunk_ids: Iterable[str],
    pool_chunk_ids: Sequence[str],
    callees_of: Callable[[str], Sequence[SymbolMember]],
    limits: GapLimits,
) -> dict[str, tuple[str, ...]]:
    """G3：被点名容器的**实现函数**——包内成员的 calls 出边里、池内但未进包的那些。

    与 G1 的区别（为什么 G1 不够）：G1 靠 ``symbols_in_container`` 找**符号表里的成员**
    （``Class.method``）。但调用链题真正要问的实现常在**模块级函数或函数内的嵌套回调**，
    它们不是任何类的成员。实测（langchain ``create_agent``）：用户问 model↔tools 边如何
    路由，答案是 ``_make_tools_to_model_edge``（模块级工厂函数，被 ``create_agent`` 调用），
    它既不是 ``create_agent`` 的成员，也不会因“同容器”被 G1 纳入——只能由 calls 出边抵达。

    三个条件（同 G1 的"不预猜意图"）：

    - **锚点**：查询点名的名字，且**包内已存在该锚点的成员**（或锚点自身）——证明这个
      容器已被判定为相关；
    - **池内**：补检目标必须是首轮召回过的候选（模块纪律 1）；
    - **未进包**：已经在包里的不重复补。

    为什么只取**已进包符号**的出边（不含锚点自身）：锚点自身的出边就是图扩展在做的
    事（``expand`` 已把 top 20 种子的 callees 拉进池），两者重复；而"已进包成员的
    被调用方"恰好是 engine 里 ``callees_of_packed`` 那层优先级的依据，与调用链断裂
    的字面含义一致。
    """
    packed_chunks = set(packed_chunk_ids)
    packed_set = {_unqualified(symbol) for symbol in packed_symbols}

    anchors: list[str] = []
    for symbol in query_symbols:
        name = _unqualified(symbol)
        if name in anchors:
            continue
        if name in packed_set or any(packed.startswith(name + ".") for packed in packed_set):
            anchors.append(symbol)

    plan: dict[str, tuple[str, ...]] = {}
    for anchor in anchors[: limits.max_containers]:
        wanted: dict[str, str] = {}
        for member in callees_of(anchor):
            if not member.chunk_id or not member.fqn:
                continue
            if member.chunk_id in packed_chunks:
                continue
            wanted.setdefault(member.chunk_id, member.fqn)
        if not wanted:
            continue
        # **保持 ``callees_of`` 给出的顺序**（即“私有实现函数优先”），不按池序重排。
        #
        # 与 G1 的关键差别：G1 的候选带首轮池序（含 rerank 相关度判定），复用它比另起排序更诚实；
        # 而 G3 的候选几乎全是图扩展进来的 tier3、 ``score`` 恒为 0.5，**池序没有区分度**，
        # 按池序截断等于随机取舍——实测把本题目标
        # （``_make_tools_to_model_edge``，字母序第 8）切掉，G3 等于白做。
        pool_set = set(pool_chunk_ids)
        ordered = [chunk_id for chunk_id in wanted if chunk_id in pool_set][
            : limits.callees_per_container
        ]
        if ordered:
            plan[anchor] = tuple(ordered)
    return plan


def _reexport_closure(
    pool_chunk_ids: Sequence[str],
    packed_chunk_ids: Iterable[str],
    reexport_marks: Mapping[str, str],
) -> dict[str, tuple[str, ...]]:
    """G4：seed 符号的**公开导出点**——已在池内（由 ``expand`` 拉入）但未进包的那些。

    与 G3 的区别：G3 补“容器调用的实现函数”，G4 补“这个符号本身在哪被公开导出”。
    后者是 imports 边（不属 calls 图），图扩展已按 ``__init__.py`` + 同名条件拉进池
    （见 ``expand._expand_reexport_sites``），但它们的分数被 ``fallback_block −0.5``
    抵掉了图连通 ``+0.5``，``score`` 归零——**必须由补检带进来**。

    ``reexport_marks``：``chunk_id -> 被导出的符号名``（由 engine 从候选的
    ``re-export site`` 标记与 seed 符号名对齐后给出）。
    """
    packed = set(packed_chunk_ids)
    plan: dict[str, tuple[str, ...]] = {}
    for chunk_id in pool_chunk_ids:
        anchor = reexport_marks.get(chunk_id)
        if anchor is None or chunk_id in packed:
            continue
        plan.setdefault(anchor, [])
        if chunk_id not in plan[anchor]:
            plan[anchor] = (*plan[anchor], chunk_id)
    return {k: tuple(v) for k, v in plan.items()}


def _spec_anchor_closure(
    packed_spec_refs: Mapping[str, Sequence[str]],
    packed_chunk_ids: Iterable[str],
    pool_chunk_ids: Iterable[str],
    limits: GapLimits,
) -> dict[str, tuple[str, ...]]:
    """G2：包内 spec 块 → 它 ``spec_references`` 指向的、**池内**但未进包的代码符号。

    ``spec_refs_for_spec(spec_block_id)`` 返回的 ``symbol_id`` 就是目标 chunk id
    （``docs/design`` 与 ``expand._expand_spec_seed`` 同一口径：符号有切片时
    ``chunk_id = symbol_id``）。

    实测缺口（``cockpit-0033``）：docs 里的"输入准入"一节进包并排第 1（score 3.26），
    它引用的 ``src/cvi_agent_core/runtime/runtime.py:Runtime.admit``（池内 rank 26）
    与 ``dispatcher.py:InputDispatcher``（rank 19）却因低于相对分数闸门而未进包——
    文档在包里、它指向的代码不在，调用链因此断裂。
    """
    packed = set(packed_chunk_ids)
    pool = set(pool_chunk_ids)
    plan: dict[str, tuple[str, ...]] = {}
    for anchor, refs in packed_spec_refs.items():
        if len(plan) >= limits.max_spec_anchors:
            break
        # 按首轮池序取（池序已含 rerank 的相关度判定，直接复用它比另起一套排序更诚实）。
        # ``refs`` 已经是池序（调用方按池序给），这里只做截断 + 未进包的判重。
        usable: list[str] = []
        seen: set[str] = set()
        for ref in refs:
            if ref in packed or ref not in pool or ref in seen:
                continue
            seen.add(ref)
            usable.append(ref)
            if len(usable) >= limits.refs_per_spec:
                break
        if usable:
            plan[anchor] = tuple(usable)
    return plan


# --------------------------------------------------------------------------- 主入口


def plan_gaps(
    query: str,
    *,
    packed_symbols: Iterable[str],
    packed_chunk_ids: Iterable[str],
    pool_chunk_ids: Iterable[str],
    packed_spec_refs: Mapping[str, Sequence[str]],
    members_of: Callable[[str], Sequence[SymbolMember]],
    callees_of: Callable[[str], Sequence[SymbolMember]] | None = None,
    reexport_marks: Mapping[str, str] | None = None,
    limits: GapLimits | None = None,
) -> GapPlan:
    """首轮包 + 候选池 → 补检计划（纯函数，无 I/O，无 LLM）。

    调用方负责取数据（``engine`` 在 ``search_with_trace`` 里从同一个 ``Store`` 取）：

    - ``packed_symbols``：包内**代码**证据的符号名（``EvidenceItem.symbol``）；
    - ``packed_chunk_ids``：包内全部证据对应的 ``chunk_id``（代码 + spec），用于判重；
    - ``pool_chunk_ids``：首轮候选池的 ``chunk_id``（rerank 后顺序，决定补检优先级）；
    - ``packed_spec_refs``：``spec chunk_id -> spec_references 指向的 chunk_id``；
    - ``members_of``：容器 → 成员符号（``Store.symbols_in_container``；测试可注入假实现）；
    - ``callees_of``：容器 → 它的实现函数（G3；``None`` = 不启用 G3）。

    返回的 ``GapPlan`` 只含**池内**的 ``chunk_id``（模块纪律 1），且按上限截断。
    """
    active = limits or GapLimits()
    packed_chunk_set = set(packed_chunk_ids)
    named = _query_symbols(query)
    container_plan = _named_containers(
        query_symbols=named,
        packed_symbols=packed_symbols,
        packed_chunk_ids=packed_chunk_set,
        pool_chunk_ids=pool_chunk_ids,
        members_of=members_of,
        limits=active,
    )
    callee_plan = (
        _callee_closure(
            query_symbols=named,
            packed_symbols=packed_symbols,
            packed_chunk_ids=packed_chunk_set,
            pool_chunk_ids=pool_chunk_ids,
            callees_of=callees_of,
            limits=active,
        )
        if callees_of is not None
        else {}
    )
    spec_plan = _spec_anchor_closure(
        packed_spec_refs, packed_chunk_set, pool_chunk_ids, active
    )
    reexport_plan = _reexport_closure(pool_chunk_ids, packed_chunk_set, reexport_marks or {})

    total = 0
    trimmed_containers: dict[str, list[str]] = {}
    trimmed_callees: dict[str, list[str]] = {}
    trimmed_spec: dict[str, list[str]] = {}
    trimmed_reexport: dict[str, list[str]] = {}

    # 总额截断用**跨规则轮转**（round-robin）而不是"先 G1 再 G2"：
    # 两条规则是**不同类**的缺口（容器断层 / 文档闭包），顺序优先会把后一条完全饿死。
    # 实测：``max_total=6`` 而容器有 10 个成员时，顺序优先使 G2 得 0 个——
    # 而 G2 恰恰是调用链断裂那一类（``cockpit-0033``）。轮转让两条规则都能落地。
    #
    # 规则顺序（TASK-MCP-CHAIN）：G4（导出点，最多 1 条、名字精确匹配、无替代证据）
    # 排在轮转最前——它是“公开 API 在哪导出”这类问题的**唯一答案**，
    # 而后三条各有多个候选、靠排序质量取胜。
    groups: list[tuple[dict[str, list[str]], str]] = (
        [(trimmed_reexport, anchor) for anchor in reexport_plan]
        + [(trimmed_containers, anchor) for anchor in container_plan]
        + [(trimmed_callees, anchor) for anchor in callee_plan]
        + [(trimmed_spec, anchor) for anchor in spec_plan]
    )
    cursors = [0] * len(groups)
    remaining = True
    while total < active.max_total and remaining:
        remaining = False
        for index, (target, _anchor) in enumerate(groups):
            if total >= active.max_total:
                break
            source_group = (
                reexport_plan
                if index < len(reexport_plan)
                else container_plan
                if index < len(reexport_plan) + len(container_plan)
                else callee_plan
                if index < len(reexport_plan) + len(container_plan) + len(callee_plan)
                else spec_plan
            )
            items = source_group[_anchor]
            cursor = cursors[index]
            if cursor >= len(items):
                continue
            remaining = True
            target.setdefault(_anchor, []).append(items[cursor])
            cursors[index] = cursor + 1
            total += 1

    return GapPlan(
        container_members={k: tuple(v) for k, v in trimmed_containers.items() if v},
        spec_refs={k: tuple(v) for k, v in trimmed_spec.items() if v},
        callee_refs={k: tuple(v) for k, v in trimmed_callees.items() if v},
        reexport_sites={k: tuple(v) for k, v in trimmed_reexport.items() if v},
    )


def _query_symbols(query: str) -> tuple[str, ...]:
    """查询里的符号（Explicit + Inferred，与 rerank 的 ``query_symbols`` 同一口径）。

    从 retrieval 内部复用 ``exact`` 的解析器（同层依赖，不引入新耦合）：
    Explicit 是反引号包裹/带 ``::`` 的点名；Inferred 是从自然语言抽出的驼峰/蛇形词。
    """
    from nova_core.retrieval.exact import extract_inferred, parse_explicit

    explicit = parse_explicit(query)
    symbols: list[str] = list(explicit.symbols)
    for token in extract_inferred(query):
        if token not in symbols:
            symbols.append(token)
    return tuple(symbols)
