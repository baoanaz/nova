"""TASK-011 确定性 rerank（DoD：12 条特征逐项正/负例、reasons 可解释、§4.6 组合回归）。"""

from __future__ import annotations

import pytest
from nova_core.retrieval.expand import GRAPH_REASON_PREFIX, SYNTHESIZED_REASON
from nova_core.retrieval.rerank import (
    FEATURE_CONSENSUS3,
    FEATURE_DEPRECATED_PATH,
    FEATURE_DOCTYPE,
    FEATURE_ENTRY_POINT,
    FEATURE_EXPLICIT,
    FEATURE_FALLBACK,
    FEATURE_GENERATED,
    FEATURE_GRAPH_1HOP,
    FEATURE_GRAPH_2HOP,
    FEATURE_NAMES,
    FEATURE_STALE_SPEC,
    FEATURE_SYMBOL_MATCH,
    FEATURE_SYNTHESIZED,
    FEATURE_TEST_FIXTURE,
    FEATURE_VECTOR_NEAR,
    FEATURE_VECTOR_TOP,
    RRF_BASE_SCALE,
    RerankSignals,
    RerankWeights,
    collect_signals,
    features,
    is_generated_path,
    is_test_intent,
    rerank,
    with_weights,
)
from nova_core.types import Candidate, SpecBlockDef


def _candidate(
    chunk_id: str = "src/a.py:f:1",
    *,
    rrf_score: float = 0.0,
    tier: int = 1,
    channel_ranks: dict[str, int] | None = None,
    reasons: list[str] | None = None,
    kind: str = "code",
    symbol_fqn: str | None = "f",
    path: str | None = "src/a.py",
    graph_depth: int = 0,
) -> Candidate:
    return Candidate(
        chunk_id=chunk_id,
        kind=kind,
        rrf_score=rrf_score,
        score=rrf_score,
        channel_ranks=dict(channel_ranks or {"bm25": 1}),
        tier=tier,
        reasons=list(reasons or []),
        symbol_fqn=symbol_fqn,
        path=path,
        start_line=1,
        end_line=3,
        graph_depth=graph_depth,
    )


_NO_SIGNALS = RerankSignals()


def _hits(candidate: Candidate, signals: RerankSignals | None = None) -> dict[str, float]:
    """命中的特征名 → 分值（DoD：每条特征都要有正/负例断言）。"""
    return dict(features(candidate, signals or _NO_SIGNALS, RerankWeights()))


def _score(candidate: Candidate, signals: RerankSignals | None = None) -> float:
    return sum(_hits(candidate, signals).values())


# --------------------------------------------------------------------------- 12 条特征

def test_feature_explicit_hit_positive_and_negative() -> None:
    explicit = _candidate(tier=0, channel_ranks={"exact": 1})
    assert _hits(explicit) == {FEATURE_EXPLICIT: 2.0}
    plain = _candidate(tier=1, channel_ranks={"bm25": 1})
    assert _hits(plain) == {}


def test_feature_explicit_path_reason_counts() -> None:
    candidate = _candidate(reasons=["explicit path"], tier=0)
    assert _hits(candidate) == {FEATURE_EXPLICIT: 2.0}


def test_feature_symbol_match_positive_and_negative() -> None:
    signals = RerankSignals(query_symbols=frozenset({"TokenService::refresh"}))
    matched = _candidate(symbol_fqn="TokenService.refresh")
    assert _hits(matched, signals) == {FEATURE_SYMBOL_MATCH: 1.0}
    other = _candidate(symbol_fqn="RefreshUI.refresh")
    assert FEATURE_SYMBOL_MATCH not in _hits(other, signals)


def test_feature_consensus3_positive_and_negative() -> None:
    three = _candidate(channel_ranks={"exact": 1, "bm25": 2, "vector": 30})
    assert _hits(three) == {FEATURE_EXPLICIT: 2.0, FEATURE_CONSENSUS3: 0.5}
    two = _candidate(channel_ranks={"bm25": 1, "vector": 40})
    assert _hits(two) == {}


def test_feature_vector_semantic_rank(TASK_105=False) -> None:
    """向量通道排名作为语义相关度证据（TASK-105）。

    分两档：top-8 满分、9-10 次档；窗口外（≥ 11）不加分，避免把"排得很后面的语义噪声"
    当证据。向量 rank 与三通道共识独立叠加。
    """
    top = _candidate(channel_ranks={"vector": 1})
    assert _hits(top) == {FEATURE_VECTOR_TOP: 1.5}
    boundary = _candidate(channel_ranks={"vector": 8})
    assert _hits(boundary) == {FEATURE_VECTOR_TOP: 1.5}
    near = _candidate(channel_ranks={"vector": 9})
    assert _hits(near) == {FEATURE_VECTOR_NEAR: 0.7}
    edge = _candidate(channel_ranks={"vector": 10})
    assert _hits(edge) == {FEATURE_VECTOR_NEAR: 0.7}
    outside = _candidate(channel_ranks={"vector": 11})
    assert _hits(outside) == {}
    # 语义相关性比"通道数多"更接近用户意图：单通道 vector rank 1 应胜过双通道平庸命中。
    semantic_only = _candidate("src/a.py:f:1", rrf_score=1 / 61, channel_ranks={"vector": 1})
    weak_pair = _candidate(
        "src/b.py:g:1", rrf_score=2 / 61, channel_ranks={"bm25": 30, "literal": 30}
    )
    ranked = rerank([semantic_only, weak_pair])
    assert ranked[0].chunk_id == semantic_only.chunk_id


def test_feature_graph_1hop_top1_positive_and_negative() -> None:
    seed_id = "src/seed.py:s:1"
    signals = RerankSignals(top1_seed_chunk_id=seed_id)
    connected = _candidate(
        "src/n.py:n:1", reasons=[f"{GRAPH_REASON_PREFIX}{seed_id}"], graph_depth=1
    )
    assert _hits(connected, signals) == {FEATURE_GRAPH_1HOP: 0.5}
    other_parent = _candidate(
        "src/n.py:n:1", reasons=[f"{GRAPH_REASON_PREFIX}src/other.py:o:1"], graph_depth=1
    )
    assert FEATURE_GRAPH_1HOP not in _hits(other_parent, signals)


def test_feature_doctype_positive_and_negative() -> None:
    design = _candidate(
        "docs/design/x.md:h:1", kind="spec", path="docs/design/x.md", symbol_fqn="h"
    )
    assert _hits(design) == {FEATURE_DOCTYPE: 0.8}
    guide = _candidate("notes/x.md:h:1", kind="spec", path="notes/x.md", symbol_fqn="h")
    assert FEATURE_DOCTYPE not in _hits(guide)


def test_feature_doctype_uses_supplied_map() -> None:
    signals = RerankSignals(doctype_by_chunk={"c": "adr"})
    candidate = _candidate("c", kind="spec", path="docs/other/x.md", symbol_fqn="h")
    assert _hits(candidate, signals) == {FEATURE_DOCTYPE: 0.8}


def test_feature_entry_point_positive_and_negative() -> None:
    signals = RerankSignals(exported_chunk_ids=frozenset({"src/a.py:f:1"}))
    assert _hits(_candidate(), signals) == {FEATURE_ENTRY_POINT: 0.2}
    assert FEATURE_ENTRY_POINT not in _hits(_candidate("src/b.py:g:1"), signals)


def test_feature_generated_negative_only() -> None:
    signals = RerankSignals(generated_paths=frozenset({"src/a.py"}))
    assert _hits(_candidate(), signals) == {FEATURE_GENERATED: -1.0}
    assert FEATURE_GENERATED not in _hits(_candidate("src/b.py:g:1", path="src/b.py"), signals)


def test_feature_test_fixture_is_suppressed_by_default() -> None:
    """TASK-108：测试夹具**常态抑制**（不是只在无测试意图时）。

    旧口径只在 ``not test_intent`` 时扣分，于是无测试意图的查询不会降测试，
    实测导致测试函数名（含 retry/idempotency 等自然语言词）挤占实现证据。
    新口径：默认降；**只有查询明示测试意图时**才不降（那时用户要的就是测试）。
    """
    test_candidate = _candidate("core/tests/test_a.py:t:1", kind="test",
                                path="core/tests/test_a.py")
    # 无信号（查询未明示测试意图）→ 抑制生效。
    assert _hits(test_candidate) == {FEATURE_TEST_FIXTURE: -1.5}
    # 查询明示测试意图 → 不抑制。
    with_intent = RerankSignals(test_intent=True)
    assert FEATURE_TEST_FIXTURE not in _hits(test_candidate, with_intent)


def test_feature_stale_spec_negative_only() -> None:
    signals = RerankSignals(stale_chunk_ids=frozenset({"c"}))
    assert _hits(_candidate("c"), signals) == {FEATURE_STALE_SPEC: -0.8}
    assert FEATURE_STALE_SPEC not in _hits(_candidate("d"), signals)


def test_feature_fallback_block_negative_only() -> None:
    assert _hits(_candidate(kind="fallback")) == {FEATURE_FALLBACK: -0.5}
    assert FEATURE_FALLBACK not in _hits(_candidate(kind="code"))


def test_feature_graph_2hop_negative_only() -> None:
    assert _hits(_candidate(graph_depth=2)) == {FEATURE_GRAPH_2HOP: -0.3}
    assert FEATURE_GRAPH_2HOP not in _hits(_candidate(graph_depth=1))


def test_feature_synthesized_edge_negative_only() -> None:
    marked = _candidate(reasons=[SYNTHESIZED_REASON], graph_depth=1)
    assert _hits(marked) == {FEATURE_SYNTHESIZED: -0.2}
    assert FEATURE_SYNTHESIZED not in _hits(_candidate(graph_depth=1))


def test_feature_names_cover_fifteen() -> None:
    """特征表规模（TASK-101 新增 2 条 literal；TASK-105 新增 2 条 vector rank；
    TASK-MCP-BUDGET 新增 1 条 deprecated path）。
    """
    assert len(FEATURE_NAMES) == 17
    assert len(set(FEATURE_NAMES)) == 17


# --------------------------------------------------------------------------- 打分与排序

def test_score_is_scaled_rrf_plus_features() -> None:
    candidate = _candidate(rrf_score=1 / 61, tier=0, channel_ranks={"exact": 1})
    reranked = rerank([candidate])[0]
    assert reranked.score == pytest.approx((1 / 61) * RRF_BASE_SCALE + 2.0)
    assert reranked.rrf_score == pytest.approx(1 / 61)  # 融合分不被改写
    assert any(FEATURE_EXPLICIT in reason for reason in reranked.reasons)


def test_reasons_are_explainable_and_unique() -> None:
    candidate = _candidate(kind="fallback")
    rerank([candidate])
    rerank([candidate])
    assert sum(FEATURE_FALLBACK in reason for reason in candidate.reasons) == 1
    assert any("-0.5" in reason for reason in candidate.reasons)


def test_consensus_beats_weak_explicit_hit_module_02_section_4_6() -> None:
    """组合回归：Explicit 命中弱相关 UI 模块 vs 三通道共识的强相关符号 → 共识者胜。"""
    weak_explicit = _candidate(
        "src/ui/refresh_panel.py:refresh:3",
        rrf_score=1 / 61,
        tier=0,
        channel_ranks={"exact": 1},
        symbol_fqn="refresh",
        path="src/ui/refresh_panel.py",
    )
    strong_consensus = _candidate(
        "src/auth/token_service.py:TokenService.refresh:45",
        rrf_score=1 / 61 + 1 / 61 + 1 / 62,
        tier=2,
        channel_ranks={"bm25": 1, "vector": 1, "inferred": 2},
        symbol_fqn="TokenService.refresh",
        path="src/auth/token_service.py",
    )
    signals = RerankSignals(query_symbols=frozenset({"TokenService::refresh"}))
    ordered = rerank([weak_explicit, strong_consensus], signals)
    assert [c.chunk_id for c in ordered] == [strong_consensus.chunk_id, weak_explicit.chunk_id]


def test_ordering_is_deterministic_on_equal_scores() -> None:
    a = _candidate("b", rrf_score=0.01)
    b = _candidate("a", rrf_score=0.01)
    assert [c.chunk_id for c in rerank([a, b])] == ["a", "b"]


def test_weights_override_does_not_change_feature_structure() -> None:
    lightweight = with_weights(RerankWeights(), explicit_hit=0.0)
    assert lightweight.explicit_hit == 0.0
    assert lightweight.symbol_match == 1.0
    assert len(RerankWeights().__dataclass_fields__) == 17
    with pytest.raises(TypeError):
        with_weights(RerankWeights(), not_a_feature=1.0)


# --------------------------------------------------------------------------- 确定性规则

def test_test_intent_word_rule() -> None:
    assert is_test_intent("这个 pytest 用例在哪") is True
    assert is_test_intent("Where is the token refresh logic") is False


@pytest.mark.parametrize(
    "path,expected",
    [
        ("src/api.pb.cc", True),
        ("src/foo_generated.py", True),
        ("gen/autogen_client.c", True),
        ("src/model_pb2.py", False),
        ("src/token_service.py", False),
    ],
)
def test_generated_path_proxy(path: str, expected: bool) -> None:
    assert is_generated_path(path) is expected


# --------------------------------------------------------------------------- collect_signals

def test_collect_signals_from_store(store, seed_file, sym) -> None:
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[
            sym("refresh", "TokenService.refresh", kind="method", start=45, is_exported=True),
            sym("rotate", "TokenStore.rotate", kind="method", start=200),
        ],
    )
    seed_file(
        store,
        path="src/api.pb.cc",
        language="cpp",
        symbols=[sym("proto", "proto", start=1)],
    )
    seed_file(
        store,
        path="docs/design/auth.md",
        language="markdown",
        spec_blocks=[
            SpecBlockDef(
                path="docs/design/auth.md",
                heading="Refresh",
                heading_path="认证 > Refresh",
                level=2,
                start_line=10,
                end_line=20,
                content="设计说明\n",
                doctype="design",
            )
        ],
    )
    store.add_spec_refs([("docs/design/auth.md:认证 > Refresh:10",
                          "src/auth/token_service.py:TokenService.refresh:45")])
    store.apply_deletions(["src/gone.py"])

    code = _candidate(
        "src/auth/token_service.py:TokenService.refresh:45",
        tier=0,
        channel_ranks={"exact": 1, "bm25": 1},
        symbol_fqn="TokenService.refresh",
        path="src/auth/token_service.py",
    )
    code.score = 0.05
    proto = _candidate("src/api.pb.cc:proto:1", symbol_fqn="proto", path="src/api.pb.cc")
    proto.score = 0.01
    spec = _candidate(
        "docs/design/auth.md:认证 > Refresh:10",
        kind="spec",
        symbol_fqn="认证 > Refresh",
        path="docs/design/auth.md",
    )
    spec.score = 0.02

    signals = collect_signals(store, "`TokenService::refresh` 怎么刷新", [code, proto, spec])
    assert signals.explicit_chunk_ids == {code.chunk_id}
    assert signals.top1_seed_chunk_id == code.chunk_id
    assert code.chunk_id in signals.exported_chunk_ids
    assert "src/api.pb.cc" in signals.generated_paths  # 文件名约定代理
    assert signals.doctype_by_chunk[spec.chunk_id] == "design"
    assert signals.test_intent is False
    assert "TokenService::refresh" in signals.query_symbols


def test_collect_signals_stale_spec_chunk(store, seed_file, sym) -> None:
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[sym("refresh", "TokenService.refresh", kind="method", start=45)],
    )
    seed_file(
        store,
        path="docs/auth.md",
        language="markdown",
        spec_blocks=[
            SpecBlockDef(
                path="docs/auth.md",
                heading="Refresh",
                heading_path="认证 > Refresh",
                level=2,
                start_line=10,
                end_line=20,
                content="说明\n",
                doctype="design",
            )
        ],
    )
    store.add_spec_refs([("docs/auth.md:认证 > Refresh:10",
                          "src/auth/token_service.py:TokenService.refresh:45")])
    store.apply_deletions(["src/auth/token_service.py"])

    spec = _candidate("docs/auth.md:认证 > Refresh:10", kind="spec", path="docs/auth.md")
    signals = collect_signals(store, "为什么这样设计", [spec])
    assert signals.stale_chunk_ids == {spec.chunk_id}
    hit_names = [name for name, _value in features(spec, signals, RerankWeights())]
    assert FEATURE_STALE_SPEC in hit_names
    assert _score(spec, signals) == pytest.approx(-0.8)


def test_collect_signals_test_intent_from_test_symbol(store, seed_file, sym) -> None:
    seed_file(
        store,
        path="core/tests/test_token.py",
        symbols=[sym("testRefresh", "testRefresh", start=1)],
    )
    candidate = _candidate("core/tests/test_token.py:testRefresh:1", kind="test",
                           symbol_fqn="testRefresh", path="core/tests/test_token.py")
    signals = collect_signals(store, "testRefresh", [candidate])
    assert signals.test_intent is True
    hit_names = [name for name, _value in features(candidate, signals, RerankWeights())]
    assert FEATURE_TEST_FIXTURE not in hit_names  # fixture 惩罚被测试意图抵消
    assert FEATURE_SYMBOL_MATCH in hit_names


def test_collect_signals_overrides(store) -> None:
    candidate = _candidate("src/a.py:f:1", path="src/a.py")
    signals = collect_signals(
        store,
        "refresh_token",
        [candidate],
        test_intent=True,
        generated_paths={"src/a.py"},
        doctype_by_chunk={"src/a.py:f:1": "adr"},
    )
    assert signals.test_intent is True
    assert signals.generated_paths == {"src/a.py"}
    assert signals.doctype_by_chunk["src/a.py:f:1"] == "adr"


# --------------------------------------------------- 已弃用符号降权（TASK-MCP-BUDGET）


def test_deprecated_symbol_is_penalised_and_active_twin_wins(
    store, seed_file, sym
) -> None:
    """同符号在兼容层与活跃包各一份时，被 ``@deprecated`` 标注的那份降权。

    实测来源（langchain，2026-09-16）：``init_chat_model`` 在 ``langchain_classic``（兼容层，
    带 ``@deprecated(...)``）与 ``langchain_v1``（活跃）各一份，两者分数只差 0.006，
    兼容层因 ``inferred`` 通道排名靠前而稳定胜出 → Agent 拿已弃用实现做结论。
    """
    # 兼容层：带 @deprecated 装饰器
    seed_file(
        store,
        path="libs/langchain_classic/chat_models/base.py",
        symbols=[sym("init_chat_model", "init_chat_model", start=72, end=72)],
        bodies={"init_chat_model": "@deprecated(since='1.0.5')\ndef init_chat_model(...):\n"},
    )
    # 活跃包：同名同 fqn，但没有装饰器
    seed_file(
        store,
        path="libs/langchain_v1/langchain/chat_models/base.py",
        symbols=[sym("init_chat_model", "init_chat_model", start=194, end=194)],
        bodies={"init_chat_model": "def init_chat_model(...):\n"},
    )
    old = _candidate(
        "libs/langchain_classic/chat_models/base.py:init_chat_model:72",
        rrf_score=0.0323,
        path="libs/langchain_classic/chat_models/base.py",
        symbol_fqn="init_chat_model",
    )
    new = _candidate(
        "libs/langchain_v1/langchain/chat_models/base.py:init_chat_model:194",
        rrf_score=0.0315,
        path="libs/langchain_v1/langchain/chat_models/base.py",
        symbol_fqn="init_chat_model",
    )
    signals = collect_signals(store, "init_chat_model 的 provider 推断", [old, new])
    assert signals.deprecated_chunk_ids, "必须识别出带 @deprecated 的符号"
    ranked = rerank([old, new], signals)
    assert ranked[0].path.startswith("libs/langchain_v1/"), (
        f"活跃实现应胜出，实际：{[c.path for c in ranked]}"
    )
    reasons = {c.chunk_id: c.reasons for c in ranked}
    assert any(FEATURE_DEPRECATED_PATH in r for r in reasons[old.chunk_id])
    assert not any(FEATURE_DEPRECATED_PATH in r for r in reasons[new.chunk_id])


def test_deprecated_penalty_covers_sibling_overload_chunks(store, seed_file, sym) -> None:
    """``@deprecated`` 只标注在最后一个 overload 上时，同文件同符号的其它切片一并降权。

    实测踩到：Python 的 ``@overload`` 把一个函数拆成多个 chunk（``init_chat_model`` 在
    classic 有 L36/47/58/72），装饰器只在 L72 那个切片首行。若只降权命中切片，
    排第 1 的 L36 原封不动 → 整个修复失效。
    """
    seed_file(
        store,
        path="pkg/compat/base.py",
        symbols=[sym("init_chat_model", "init_chat_model", start=36, end=36)],
        bodies={"init_chat_model": "@overload\ndef init_chat_model(...):\n"},
    )
    seed_file(
        store,
        path="pkg/compat/base.py",
        symbols=[sym("init_chat_model_dep", "init_chat_model", start=72, end=72)],
        bodies={"init_chat_model": "@deprecated(since='1.0')\ndef init_chat_model(...):\n"},
    )
    plain = _candidate("pkg/compat/base.py:init_chat_model:36", path="pkg/compat/base.py",
                       symbol_fqn="init_chat_model")
    marked = _candidate("pkg/compat/base.py:init_chat_model:72", path="pkg/compat/base.py",
                        symbol_fqn="init_chat_model")
    signals = collect_signals(store, "init_chat_model", [plain, marked])
    # 两个切片同文件同符号 → 都被降权（含未被直接标注的 overload 切片）
    assert plain.chunk_id in signals.deprecated_chunk_ids
    assert marked.chunk_id in signals.deprecated_chunk_ids


def test_deprecated_penalty_does_not_leak_across_files(store, seed_file, sym) -> None:
    """降权不得跨文件扩散：模块级函数 ``symbol_fqn`` 是裸名，同名的活跃实现不能被误降。

    实测踩到：只按 ``symbol_fqn`` 扩展会把 v1 的 ``init_chat_model`` 一起降权，
    两边同降 = 排名不变，修复失效。
    """
    seed_file(
        store,
        path="pkg/compat/base.py",
        symbols=[sym("init_chat_model", "init_chat_model", start=72, end=72)],
        bodies={"init_chat_model": "@deprecated(since='1.0')\ndef init_chat_model(...):\n"},
    )
    seed_file(
        store,
        path="pkg/active/base.py",
        symbols=[sym("init_chat_model", "init_chat_model", start=194, end=194)],
        bodies={"init_chat_model": "def init_chat_model(...):\n"},
    )
    old = _candidate("pkg/compat/base.py:init_chat_model:72", path="pkg/compat/base.py",
                     symbol_fqn="init_chat_model")
    new = _candidate("pkg/active/base.py:init_chat_model:194", path="pkg/active/base.py",
                     symbol_fqn="init_chat_model")
    signals = collect_signals(store, "init_chat_model", [old, new])
    assert old.chunk_id in signals.deprecated_chunk_ids
    assert new.chunk_id not in signals.deprecated_chunk_ids, "活跃实现不得被跨文件误降"


def test_deprecation_penalty_is_off_when_nothing_marked(store, seed_file, sym) -> None:
    """没有带装饰器的符号时该特征完全静默（不影响任何既有排序）。"""
    seed_file(
        store,
        path="src/a.py",
        symbols=[sym("f", "f", start=1, end=1)],
        bodies={"f": "def f():\n"},
    )
    candidate = _candidate("src/a.py:f:1", path="src/a.py", symbol_fqn="f")
    signals = collect_signals(store, "f", [candidate])
    assert signals.deprecated_chunk_ids == frozenset()
    assert not any(FEATURE_DEPRECATED_PATH in r for r in candidate.reasons)
