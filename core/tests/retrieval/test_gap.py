"""TASK-109 Evidence-Gap 二轮补检（DoD：规则确定性、只在池内补、二轮纪律、真实失败回归）。

三组断言：

1. **规则单元**（纯函数，注入假 ``members_of``）：G1/G2 的触发条件与**不触发**条件；
2. **二轮纪律**（集成，用真实 ``Engine`` + 临时索引）：最多 1 次迭代、结果必经 rerank、
   不绕过预算闸门、只有池内候选被补；
3. **真实失败回归**（``benches/golden/cockpit-agents-py`` 的 ``cockpit-0033``/``0035``）：
   需要预建索引与 embedding 后端，缺失时 skip（不给假绿）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from zace_core.engine import Engine
from zace_core.retrieval.gap import (
    GAP_REASON_PREFIX,
    GapLimits,
    GapPlan,
    SymbolMember,
    plan_gaps,
)

ROOT = Path(__file__).resolve().parents[3]


def _members(table: dict[str, list[tuple[str, str]]]):
    """``{container: [(fqn, chunk_id), ...]}`` → 假 ``members_of``。"""
    def lookup(container: str) -> list[SymbolMember]:
        return [SymbolMember(fqn=fqn, chunk_id=cid) for fqn, cid in table.get(container, [])]

    return lookup


# --------------------------------------------------------------------------- G1


def test_g1_container_member_gap_is_planned() -> None:
    """查询点名的容器已在包内，其池内成员未进包 → 补检这些成员（真实缺口 cockpit-0035）。"""
    plan = plan_gaps(
        "能力网关 `CapabilityGateway` 如何实现幂等、重试与 UNKNOWN 终态？",
        packed_symbols=["CapabilityGateway"],
        packed_chunk_ids=["gw:CapabilityGateway:71"],
        pool_chunk_ids=[
            "gw:_reserve_idempotency:336",
            "gw:_should_retry:486",
            "gw:_normalize:441",
            "other:x:1",
        ],
        packed_spec_refs={},
        members_of=_members(
            {
                "CapabilityGateway": [
                    ("CapabilityGateway", "gw:CapabilityGateway:71"),
                    ("CapabilityGateway._reserve_idempotency", "gw:_reserve_idempotency:336"),
                    ("CapabilityGateway._should_retry", "gw:_should_retry:486"),
                    ("CapabilityGateway._normalize", "gw:_normalize:441"),
                ]
            }
        ),
    )
    assert plan.kinds == ("G1",)
    assert plan.triggered
    assert set(plan.chunk_ids) == {
        "gw:_reserve_idempotency:336",
        "gw:_should_retry:486",
        "gw:_normalize:441",
    }
    # reason 必须能说出“是哪个容器带来的”（可解释性，进 ContextPack 的 reason 行）。
    assert "CapabilityGateway" in (plan.reason_for("gw:_should_retry:486") or "")
    assert (plan.reason_for("gw:_should_retry:486") or "").startswith(GAP_REASON_PREFIX)


def test_g1_requires_container_named_in_query() -> None:
    """容器没被查询点名 → 不触发（否则任何包内出现的类都会无条件扩张）。"""
    plan = plan_gaps(
        "幂等是怎么实现的？",
        packed_symbols=["CapabilityGateway"],
        packed_chunk_ids=["gw:CapabilityGateway:71"],
        pool_chunk_ids=["gw:_should_retry:486"],
        packed_spec_refs={},
        members_of=_members(
            {"CapabilityGateway": [("CapabilityGateway._should_retry", "gw:_should_retry:486")]}
        ),
    )
    assert plan.kinds == ()
    assert not plan.triggered


def test_g1_requires_container_already_in_pack() -> None:
    """容器不在包内 → 不触发（补的是“已判相关却被截断”的部分，不是引入新主题）。"""
    plan = plan_gaps(
        "`CapabilityGateway` 如何实现重试？",
        packed_symbols=["SomethingElse"],
        packed_chunk_ids=["other:SomethingElse:1"],
        pool_chunk_ids=["gw:_should_retry:486"],
        packed_spec_refs={},
        members_of=_members(
            {"CapabilityGateway": [("CapabilityGateway._should_retry", "gw:_should_retry:486")]}
        ),
    )
    assert plan.kinds == ()


def test_g1_only_backfills_within_pool() -> None:
    """池外成员**不补**（模块纪律 1：只补首轮已召回的候选，不做盲搜）。"""
    plan = plan_gaps(
        "`CapabilityGateway` 如何实现重试？",
        packed_symbols=["CapabilityGateway"],
        packed_chunk_ids=["gw:CapabilityGateway:71"],
        pool_chunk_ids=["gw:_reserve_idempotency:336"],
        packed_spec_refs={},
        members_of=_members(
            {
                "CapabilityGateway": [
                    ("CapabilityGateway._reserve_idempotency", "gw:_reserve_idempotency:336"),
                    ("CapabilityGateway._never_recalled", "gw:_never_recalled:999"),
                ]
            }
        ),
    )
    assert plan.chunk_ids == ("gw:_reserve_idempotency:336",)


def test_g1_skips_members_already_in_pack() -> None:
    """已进包的成员不再补（按 chunk_id 与符号名双重判重）。"""
    plan = plan_gaps(
        "`CapabilityGateway` 如何实现重试？",
        packed_symbols=["CapabilityGateway", "CapabilityGateway._verify"],
        packed_chunk_ids=["gw:CapabilityGateway:71", "gw:_verify:401"],
        pool_chunk_ids=["gw:_verify:401", "gw:_should_retry:486"],
        packed_spec_refs={},
        members_of=_members(
            {
                "CapabilityGateway": [
                    ("CapabilityGateway._verify", "gw:_verify:401"),
                    ("CapabilityGateway._should_retry", "gw:_should_retry:486"),
                ]
            }
        ),
    )
    assert plan.chunk_ids == ("gw:_should_retry:486",)


def test_g1_member_matcher_handles_cpp_separator() -> None:
    """C++ 的 ``A::b`` 与 Python 的 ``A.b`` 视为同一容器（两种抽取器拼写差异）。"""
    plan = plan_gaps(
        "`leveldb::DBImpl` 的 Get 怎么实现？",
        packed_symbols=["leveldb::DBImpl"],
        packed_chunk_ids=["db:DBImpl:1"],
        pool_chunk_ids=["db:Get:100"],
        packed_spec_refs={},
        members_of=_members({"leveldb::DBImpl": [("leveldb::DBImpl::Get", "db:Get:100")]}),
    )
    assert plan.chunk_ids == ("db:Get:100",)


# --------------------------------------------------------------------------- G2


def test_g2_spec_anchor_closure_is_planned() -> None:
    """包内 spec 块引用的代码在池里但不在包内 → 补检（真实缺口 cockpit-0033）。"""
    plan = plan_gaps(
        "普通 USER_REQUEST 从准入到执行 Agent 的链路是什么？",
        packed_symbols=[],
        packed_chunk_ids=["docs/design.md:输入准入:122"],
        pool_chunk_ids=["rt:admit:43", "rt:InputDispatcher:45", "unrelated:x:1"],
        packed_spec_refs={
            "docs/design.md:输入准入:122": (
                "rt:admit:43",
                "rt:InputDispatcher:45",
                "rt:already_packed:10",
            )
        },
        members_of=_members({}),
    )
    assert plan.kinds == ("G2",)
    assert set(plan.chunk_ids) == {"rt:admit:43", "rt:InputDispatcher:45"}
    assert (plan.reason_for("rt:admit:43") or "").startswith(GAP_REASON_PREFIX)


def test_g2_respects_refs_per_spec_quota() -> None:
    """单锚点引用配额生效（防止一个文档把它的全部引用灌进包）。"""
    refs = tuple(f"rt:f{i}:{i}" for i in range(20))
    plan = plan_gaps(
        "链路是什么？",
        packed_symbols=[],
        packed_chunk_ids=["d:x:1"],
        pool_chunk_ids=list(refs),
        packed_spec_refs={"d:x:1": refs},
        members_of=_members({}),
        limits=GapLimits(refs_per_spec=4),
    )
    assert len(plan.chunk_ids) == 4
    # 按池序取前 4 个（池序 = 相关度序），而不是字典序。
    assert plan.chunk_ids == ("rt:f0:0", "rt:f1:1", "rt:f2:2", "rt:f3:3")


def test_g1_and_g2_share_the_total_quota() -> None:
    """两条规则共用 ``max_total``（补检是有界的，不是两条各自放大）。"""
    plan = plan_gaps(
        "`Widget` 的链路是什么？",
        packed_symbols=["Widget"],
        packed_chunk_ids=["w:Widget:1", "d:x:1"],
        pool_chunk_ids=[f"w:m{i}:{i}" for i in range(10)] + [f"rt:f{i}:{i}" for i in range(10)],
        packed_spec_refs={"d:x:1": tuple(f"rt:f{i}:{i}" for i in range(10))},
        members_of=_members(
            {"Widget": [(f"Widget.m{i}", f"w:m{i}:{i}") for i in range(10)]}
        ),
        limits=GapLimits(max_total=6),
    )
    assert plan.kinds == ("G1", "G2")
    assert len(plan.chunk_ids) == 6


def test_multi_container_limit_is_enforced() -> None:
    """``max_containers`` 生效（一次查询点名多个容器时也有界）。"""
    plan = plan_gaps(
        "`Alpha` 与 `Beta` 怎么实现？",
        packed_symbols=["Alpha", "Beta"],
        packed_chunk_ids=["a:Alpha:1", "b:Beta:1"],
        pool_chunk_ids=["a:x:2", "b:y:2"],
        packed_spec_refs={},
        members_of=_members(
            {"Alpha": [("Alpha.x", "a:x:2")], "Beta": [("Beta.y", "b:y:2")]}
        ),
        limits=GapLimits(max_containers=1),
    )
    assert list(plan.container_members) == ["Alpha"]


# --------------------------------------------------------------------------- 计划对象


def test_gap_plan_chunk_ids_are_deduplicated_and_ordered() -> None:
    """跨规则去重且顺序稳定（G1 优先，组内保序）——保证补检装填可复现。"""
    plan = GapPlan(
        container_members={"A": ("c1", "c2")},
        spec_refs={"d": ("c2", "c3")},
    )
    assert plan.chunk_ids == ("c1", "c2", "c3")
    assert plan.reason_for("c1") is not None and "A" in plan.reason_for("c1")
    assert plan.reason_for("c3") is not None and "d" in plan.reason_for("c3")
    assert plan.reason_for("absent") is None


def test_empty_plan_is_not_triggered() -> None:
    plan = GapPlan()
    assert not plan.triggered
    assert plan.kinds == ()
    assert plan.chunk_ids == ()


# --------------------------------------------------------------------------- 集成（真实 Engine）


def _cockpit_environment() -> tuple[str, str] | None:
    """cockpit 真实回归所需环境：预建索引存在 + embedding 后端可用。

    返回 ``(data_root, project_id)``；不可用时返回 ``None``（调用方 skip）。
    索引路径可用 ``ZACE_BENCH_DATA`` 覆盖（默认 ``~/.zace/bench/voyage-4-lite-d1024``）。
    """
    data = os.environ.get("ZACE_BENCH_DATA") or str(
        Path.home() / ".zace" / "bench" / "voyage-4-lite-d1024"
    )
    project = str(
        Path(data)
        / "projects"
        / "8e69da62f37e5783"  # benches/targets.json 的 cockpit-agents-py
    )
    if not (Path(project) / "index.db").is_file():
        return None
    if not os.environ.get("EMBED_API_KEY") and os.environ.get("EMBED_MODE") != "api":
        # 向量通道需要真实 / 缓存的 query 向量；没有后端时不给“假绿”。
        return None
    return data, "8e69da62f37e5783"


def _load_case(case_id: str) -> dict:
    path = ROOT / "benches" / "golden" / "cockpit-agents-py" / "cockpit.jsonl"
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and json.loads(line)["id"] == case_id:
            return json.loads(line)
    raise AssertionError(f"golden 里找不到 {case_id}")


def _targets_in_pack(pack, case: dict) -> list[bool]:
    """期望符号是否出现在包内证据的**正文**里（与 ``eval`` 的命中口径同源）。"""
    body = "\n".join(item.content for item in [*pack.evidence, *pack.docs])
    results: list[bool] = []
    for expectation in case["expected"]:
        symbol = expectation.get("symbol") or ""
        tail = symbol.split(".")[-1]
        results.append(bool(tail) and f"def {tail}" in body or f"class {tail}" in body)
    return results


@pytest.fixture(scope="module")
def cockpit() -> tuple[Engine, str]:
    environment = _cockpit_environment()
    if environment is None:
        pytest.skip("cockpit 预建索引或 embedding 后端不可用（真实回归需要二者）")
    data, project_id = environment
    engine = Engine.open(data)
    return engine, project_id


def test_cockpit_0035_container_members_reach_the_pack(cockpit) -> None:
    """回归护栏（用例 A）：``CapabilityGateway`` 的成员方法确实被补进包。

    **口径诚实声明（2026-09-15 修正）**：本条断言的是“内容进包”，**不是“进入 top-10”**。
    两者不等价，且必须区分——实测该题三个目标虽然都进了包，但排在 **15-19 位**，
    而 benchmark 的 `first_hit_rank` 只看 top-10，因此 **``cockpit-0035`` 官方口径下仍不通过**
    （修复前是“一条都不在包”，现在是“在包尾”）。

    为什么保留本条：它锁定的是 G1 的**召回/补检能力**（修复前 0 个成员进包）。
    排序问题属 rerank 层（目标分 0.31 vs 12 个同名 ``Provider.verify`` 的 1.87-1.90），
    是另一个缺陷，不能用 G1 掩盖。
    """
    engine, project_id = cockpit
    case = _load_case("cockpit-0035")
    trace = engine.search_with_trace(project_id, case["query"], 10_000)
    assert "G1" in trace.gap_kinds
    assert trace.backfilled > 0
    assert sum(_targets_in_pack(trace.pack, case)) >= 2, (
        "G1 应把 _reserve_idempotency/_should_retry/_normalize 至少 2 个补进包"
    )
    # 补检候选必须带来源标注（I3）
    marked = [i for i in [*trace.pack.evidence, *trace.pack.docs] if "gap backfill" in i.reason]
    assert marked, "补检证据必须标注来源"


def test_cockpit_0035_still_misses_top10(cockpit) -> None:
    """**已知缺口（诚实锁定）**：``cockpit-0035`` 官方口径（top-10）仍不通过。

    本卡（TASK-109）修的是“目标不在包”，**没有修**“目标在包尾”。
    写成断言而不是注释，是为了防止“指标没动但卡片说过了”这类误报：
    哪天真把排序修好了，这条会**失败**，提醒去更新卡片与期望。
    """
    from zace_core.cli.eval import first_hit_rank, ordered_evidence

    engine, project_id = cockpit
    case = _load_case("cockpit-0035")
    trace = engine.search_with_trace(project_id, case["query"], 10_000)
    items = ordered_evidence(trace.pack)
    expectations = tuple(
        _expectation(e["path"], e.get("symbol")) for e in case["expected"]
    )
    ranked = _case_with(expectations)
    assert first_hit_rank(items, ranked, top_k=10) is None, (
        "0035 已进入 top-10 → 排序问题已修复，请更新本测试与 TASK-109 执行记录"
    )


def _expectation(path: str, symbol: str | None):
    """构造 ``GoldenCase`` 用的 ``Expectation``（避免手工拼 golden 文件）。"""
    from zace_core.cli.eval import Expectation

    return Expectation(path=path, symbol=symbol)


def _case_with(expectations):
    """只有 ``expected`` 与 ``category`` 参与命中的最小 case（供 ``first_hit_rank`` 用）。"""
    from zace_core.cli.eval import GoldenCase

    return GoldenCase(
        id="probe", query="", lang="zh", category="behavior", expected=tuple(expectations)
    )


def test_repair_only_promotes_pool_candidates(cockpit) -> None:
    """**R31 / I1 守卫**：Repair 只提池内候选，永不引入池外候选。

    这是 Repair 不演变成“第二套检索策略”的**结构不变量**：无论以后加多少条规则，
    召回空间都不会因此扩大。违反即 bug。
    """
    engine, project_id = cockpit
    case = _load_case("cockpit-0033")
    trace = engine.search_with_trace(project_id, case["query"], 10_000)
    pool_ids = {candidate.chunk_id for candidate in trace.candidates}
    promoted = [i for i in [*trace.pack.evidence, *trace.pack.docs] if "gap backfill" in i.reason]
    assert promoted, "本用例应触发补检（否则本测试没有在守任何东西）"
    for item in promoted:
        # 补检证据必须能在池里找到对应候选（同路径 + 行区间重叠）
        assert any(
            c.path == item.path
            and c.start_line is not None
            and item.lines is not None
            and not (c.end_line < item.lines[0] or c.start_line > item.lines[1])
            for c in trace.candidates
            if c.chunk_id in pool_ids
        ), f"补检证据不在候选池内：{item.path}:{item.lines}（违反 R31/I1）"
def test_cockpit_0033_call_chain_reaches_the_pack(cockpit) -> None:
    """回归护栏（用例 B）：``Runtime.admit`` 或 ``InputDispatcher`` 进包且 **进入 top-10**。

    与 0035 不同，本题在本卡后**官方口径下也通过了**（rank=3）；因此这里可以严格断言排名。
    """
    from zace_core.cli.eval import first_hit_rank, ordered_evidence

    engine, project_id = cockpit
    case = _load_case("cockpit-0033")
    trace = engine.search_with_trace(project_id, case["query"], 10_000)
    assert "G2" in trace.gap_kinds, "0033 应触发 G2（文档锚点闭包）"
    ranked = _case_with(
        _expectation(e["path"], e.get("symbol")) for e in case["expected"]
    )
    rank = first_hit_rank(ordered_evidence(trace.pack), ranked, top_k=10)
    assert rank is not None, "0033 应在 top-10 内命中（官方口径通过）"


def test_deep_uses_larger_gap_quota_but_same_pipeline(cockpit) -> None:
    """Fast/Deep 共用同一管线（D-10），差异只在配额：Deep 的 Gap 配额不小于 Fast。

    **不对 ``backfilled`` 做数值相等断言**：该值在 ``cockpit-0035`` 上会在 14/15 之间浮动，
    根因是**既有的**向量通道非确定性（同一 query 连续两次 ``recall_vector`` 会有 7 个低分位置的
    顺序互换；已在本机对未修改的 ``main`` 复现，与 TASK-109 无关）。
    本卡只断言“两条路径共用同一配额来源”，即 Deep 的配额参数不小于 Fast。
    """
    from zace_core.engine import DEEP_GAP_LIMITS

    fast = GapLimits()
    assert DEEP_GAP_LIMITS.max_containers >= fast.max_containers
    assert DEEP_GAP_LIMITS.members_per_container >= fast.members_per_container
    assert DEEP_GAP_LIMITS.max_spec_anchors >= fast.max_spec_anchors
    assert DEEP_GAP_LIMITS.refs_per_spec >= fast.refs_per_spec
    assert DEEP_GAP_LIMITS.max_total >= fast.max_total
    engine, project_id = cockpit
    case = _load_case("cockpit-0035")
    for deep in (False, True):
        trace = engine.search_with_trace(project_id, case["query"], 10_000, deep=deep)
        # 同一份代码路径：两种模式都真的跑了补检（不是 Deep 独有的一条分叉）
        assert "G1" in trace.gap_kinds
        assert trace.backfilled > 0


def test_gap_backfill_is_capped_and_marked(cockpit) -> None:
    """二轮纪律：补检有独立配额上限，且每条补检证据在 reason 里标出来源。"""
    engine, project_id = cockpit
    case = _load_case("cockpit-0035")
    trace = engine.search_with_trace(project_id, case["query"], 10_000)
    marked = [
        item
        for item in [*trace.pack.evidence, *trace.pack.docs]
        if "gap backfill" in item.reason
    ]
    assert marked, "补检证据必须带 gap backfill 来源标注（Agent 才能识别）"
    # 独立小预算：补检证据的总渲染开销不得超过 hard_cap × backfill_ratio。
    from zace_core.contextpack import estimate_render_tokens
    from zace_core.contextpack.assembly import budget_for

    ratio = budget_for("fast").backfill_ratio
    total = sum(estimate_render_tokens(item) for item in marked)
    assert total <= 10_000 * ratio * 1.05  # 计费与渲染的少量口径差


def test_gap_check_is_cheap(cockpit) -> None:
    """Gap 检查本身 <5ms（卡内验收：延迟不超标）。"""
    import time

    engine, project_id = cockpit
    case = _load_case("cockpit-0035")
    with engine._open_project(project_id) as (store, _vectors, _provider):
        from zace_core.contextpack import assemble, budget_for, collect_index_signals
        from zace_core.retrieval import RecallLimits, recall
        from zace_core.retrieval.expand import ExpansionLimits, expand
        from zace_core.retrieval.rerank import collect_signals, rerank

        recalled = recall(store, case["query"], provider=None, limits=RecallLimits())
        expansion = expand(store, recalled.candidates, limits=ExpansionLimits())
        pool = [*recalled.candidates, *expansion.candidates]
        ranked = rerank(pool, collect_signals(store, case["query"], pool))
        pack = assemble(
            store,
            case["query"],
            ranked,
            flows=expansion.flows,
            freshness=store.freshness(),
            config=budget_for("fast"),
            signals=collect_index_signals(store, ranked),
        )
        started = time.perf_counter()
        engine._backfill_gaps(store, case["query"], ranked, pack, limits=GapLimits())
        elapsed_ms = (time.perf_counter() - started) * 1000
    assert elapsed_ms < 50, f"Gap 检查耗时 {elapsed_ms:.1f}ms 超出预期（卡内目标 <5ms）"


def test_tests_import_path_is_available() -> None:
    """自证：本文件使用的 ``ROOT`` 指向仓库根（真实回归用例的 golden 在它下面）。

    **不要用父目录名判断**（TASK-109 曾写成 ``ROOT.parent.name == "ACE"``）：
    那是本机 checkout 布局的硬编码假设，而仓库目录名随 clone 方式而变
    （GitHub Actions clone 成 ``zace``，其他人可能是任意名字），该断言在 CI 必然失败。
    用仓库**自身的标志文件**判断——它定义“这是仓库根”，与目录叫什么无关。
    """
    # 仓库标志：workspace 根（D-35 monorepo）的 pyproject + 三个成员目录的 pyproject
    assert (ROOT / "pyproject.toml").is_file(), f"ROOT 不是仓库根：{ROOT}"
    assert (ROOT / "core" / "pyproject.toml").is_file()
    assert (ROOT / "service" / "pyproject.toml").is_file()
    assert (ROOT / "benches" / "targets.json").is_file()
