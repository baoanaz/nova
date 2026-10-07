"""TASK-010 BM25 通道（DoD：中文查询 segment 后命中；FTS 原始分方向）。

TASK-020 §D 追加：查询侧噪声清洗是**纯函数**（``is_noise_token`` / ``filter_bm25_tokens``，
零 SQL），``recall_bm25`` 单次调用恒为 1 条 ``fts_search`` SQL，且**不改变 OR 召回结果**。
"""

from __future__ import annotations

import inspect

from nova_core.retrieval.bm25 import (
    bm25_query_text,
    filter_bm25_tokens,
    is_noise_token,
    recall_bm25,
)
from nova_core.retrieval.fusion import CHANNEL_BM25, TIER_SEED
from nova_core.storage import Store
from nova_core.text import segment


def test_chinese_query_hits_after_segmentation(store, seed_file, sym) -> None:
    """中文一等场景（D-20/D-45）：查询侧与索引侧同一分词器。"""
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[sym("refresh", "TokenService.refresh", kind="method", start=45)],
        bodies={
            "TokenService.refresh": (
                "def refresh(self):\n"
                "    # token 过期 后 刷新 refresh_token\n"
                "    return self.store.rotate()\n"
            )
        },
    )
    seed_file(
        store,
        path="src/ui/refresh_panel.py",
        symbols=[sym("refreshPanel", "refreshPanel", start=3)],
        bodies={"refreshPanel": "def refreshPanel():\n    return 'ui only'\n"},
    )

    candidates = recall_bm25(store, "token 过期 刷新", limit=50)
    assert [c.chunk_id for c in candidates] == ["src/auth/token_service.py:TokenService.refresh:45"]
    assert candidates[0].tier == TIER_SEED
    assert candidates[0].channel_ranks == {CHANNEL_BM25: 1}
    assert any("bm25" in reason for reason in candidates[0].reasons)


def test_identifier_query_hits(store, seed_file, sym) -> None:
    seed_file(
        store,
        path="src/a.py",
        symbols=[sym("rotate", "TokenStore.rotate", start=210)],
        bodies={"TokenStore.rotate": "def rotate(self):\n    return refresh_token\n"},
    )
    candidates = recall_bm25(store, "TokenStore.rotate", limit=50)
    assert candidates and candidates[0].chunk_id == "src/a.py:TokenStore.rotate:210"


def test_ranks_follow_bm25_order(store, seed_file, sym) -> None:
    """FTS 原始分越小越相关 → 排名 1..N 按该顺序（方向不得反转）。"""
    seed_file(
        store,
        path="src/noise.py",
        symbols=[sym("noise", "noise", start=1)],
        bodies={"noise": "def noise():\n    return 'refresh_token once'\n"},
    )
    seed_file(
        store,
        path="src/hit.py",
        symbols=[sym("hit", "hit", start=1)],
        bodies={"hit": "def hit():\n    refresh_token refresh_token refresh_token\n"},
    )
    candidates = recall_bm25(store, "refresh_token", limit=50)
    assert [c.channel_ranks[CHANNEL_BM25] for c in candidates] == list(
        range(1, len(candidates) + 1)
    )
    assert candidates[0].chunk_id == "src/hit.py:hit:1"


def test_limit_respected(store, seed_file, sym) -> None:
    for index in range(5):
        seed_file(
            store,
            path=f"src/f{index}.py",
            symbols=[sym(f"f{index}", f"f{index}", start=1)],
            bodies={f"f{index}": "def f():\n    refresh_token\n"},
        )
    assert len(recall_bm25(store, "refresh_token", limit=2)) == 2


def test_empty_query_returns_empty(store) -> None:
    assert recall_bm25(store, "", limit=50) == []
    assert recall_bm25(store, "   ", limit=50) == []


def test_bm25_query_text_strips_explicit_noise() -> None:
    assert bm25_query_text("`refresh_token`") == " refresh_token "
    assert bm25_query_text("TokenService::refresh") == "TokenService refresh"


def test_backticked_symbol_query_still_hits(store, seed_file, sym) -> None:
    """反引号/``::`` 不得让整条查询失配（jieba 会把它们切成正文给不出的 token）。"""
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[sym("refresh", "TokenService.refresh", kind="method", start=45)],
        bodies={"TokenService.refresh": "def refresh(self):\n    return refreshToken\n"},
    )
    assert [c.chunk_id for c in recall_bm25(store, "`TokenService::refresh`", limit=50)] == [
        "src/auth/token_service.py:TokenService.refresh:45"
    ]


def test_generated_files_are_not_excluded_here(store, seed_file, sym) -> None:
    """Module/02 §4.2-b：generated 文件不排除（降权是 TASK-011 rerank 的事）。"""
    seed_file(
        store,
        path="src/proto_generated.py",
        symbols=[sym("gen", "gen", start=1)],
        bodies={"gen": "def gen():\n    refresh_token\n"},
    )
    candidates = recall_bm25(store, "refresh_token", limit=50)
    assert [c.chunk_id for c in candidates] == ["src/proto_generated.py:gen:1"]


# ---------------------------------------------------------------------------
# TASK-020 §D：查询侧噪声清洗（纯函数）+ 零额外 SQL 的性能回归
# ---------------------------------------------------------------------------


def test_is_noise_token_truth_table() -> None:
    """DoD ①：对标点/空白/符号为真；对字母、数字、CJK 为假（含混合与下划线）。"""
    noisy = ["？", "：", "，", ",", ".", "!", "?", "—", "…", "《》", ":", "::", "_", " ", "\u3000"]
    meaningful = ["workflow", "令牌", "记忆系统", "1", "42", "token_1", "a", "Z"]
    assert all(is_noise_token(token) for token in noisy)
    assert not any(is_noise_token(token) for token in meaningful)
    # 空串也算噪声（无任何 alnum 字符）——由 ``filter_bm25_tokens`` 负责丢弃。
    assert is_noise_token("")


def test_filter_bm25_tokens_is_a_pure_function() -> None:
    """DoD ②：不接 ``store``（因此不可能做逐 token DF 探测），只做字符类别判定。"""
    parameters = list(inspect.signature(filter_bm25_tokens).parameters)
    assert parameters == ["tokens"], f"清洗必须是纯函数，实际参数：{parameters}"
    assert filter_bm25_tokens(["令牌", "刷新", "？", "：", ""]) == ["令牌", "刷新"]
    # 与分词器行为对齐：清洗后剩下的就是分词产物里去掉纯标点的部分（不依赖 jieba 切法）。
    tokens = segment("令牌刷新？").split()
    assert "？" in tokens
    assert filter_bm25_tokens(tokens) == [t for t in tokens if t != "？"]
    # 库外词（DF=0）不在本函数职责范围内：OR 路径上它零影响（R20 实测），不清洗。
    assert filter_bm25_tokens(["Kubernetes", "？"]) == ["Kubernetes"]


def test_recall_bm25_returns_empty_when_every_token_is_noise(store) -> None:
    """DoD ③：全 token 被过滤 → 空列表（不抛异常，也不产生查询）。"""
    for query in ("？？？", "。，！", "!?,", "   ?   "):
        assert filter_bm25_tokens(segment(query).split()) == []
        assert recall_bm25(store, query, limit=50) == []


def test_recall_bm25_does_not_probe_tokens_one_by_one(store, seed_file, sym) -> None:
    """DoD 性能回归（本卡硬要求）：单次 ``recall_bm25`` 恒为 **1 条 ``fts_search`` SQL**。

    回归锚点：被否决的实现对每个 token 额外发一条 ``fts_search(token, limit=1)`` 做 DF 探测
    （11 token ⇒ 1+11 条 SQL，实测约 7.4ms/查询）——在 OR 路径上零收益（R20）。
    这里断言 SQL 调用次数**不随 token 数增长**，即与 TASK-016 的基线一致。
    """
    seed_file(
        store,
        path="src/hit.py",
        symbols=[sym("hit", "hit", start=1)],
        bodies={"hit": "def hit():\n    # 令牌刷新\n    return refresh_token\n"},
    )
    counted = _CountingStore(store)
    long_query = "令牌 refresh_token 刷新 过期 时间 在哪里 定义 和 使用 的 workflow ？"
    expected_long = " ".join(filter_bm25_tokens(segment(long_query).split()))
    assert len(expected_long.split()) >= 6  # 前提：多 token 长查询

    short_candidates = recall_bm25(counted, "令牌", limit=50)  # type: ignore[arg-type]
    calls_for_short = list(counted.calls)
    long_candidates = recall_bm25(counted, long_query, limit=50)  # type: ignore[arg-type]
    calls_for_long = counted.calls[len(calls_for_short) :]

    assert len(calls_for_short) == 1
    assert len(calls_for_long) == 1, f"逐 token 探测回归：{calls_for_long}"
    assert calls_for_long[0][0] == expected_long
    assert calls_for_long[0][1] == 50
    assert calls_for_long[0][2] == "or"  # 生产调用方恒用 OR 语义（R20 前提）
    assert short_candidates and long_candidates  # 计数包装不改变召回行为


def test_noise_filter_does_not_change_or_results(store, seed_file, sym) -> None:
    """诚实锚点（R20 实测结论）：清洗后的 OR 召回与未清洗的 OR 召回**逐项一致**。

    标点与库外 token 在 OR 语义下零贡献，因此本卡「对 OR 召回结果无任何改变」必须被固定，
    防止后续声称不存在的质量提升；排序判别力问题按裁定转 R24。
    """
    seed_file(
        store,
        path="src/hit.py",
        symbols=[sym("hit", "hit", start=1)],
        bodies={"hit": "def hit():\n    # 令牌刷新\n    return refresh_token\n"},
    )
    seed_file(
        store,
        path="src/doc.md",
        symbols=[sym("doc", "doc", start=1)],
        bodies={"doc": "# 文档\n    # 令牌 刷新 的 说明\n"},
    )
    query = "令牌刷新？Kubernetes"  # 含标点噪声 + 库外词（DF=0）
    raw_segmented = segment(bm25_query_text(query))
    assert "？" in raw_segmented.split() and "Kubernetes" in raw_segmented.split()
    cleaned = " ".join(filter_bm25_tokens(raw_segmented.split()))

    unfiltered = store.fts_search(raw_segmented, limit=50)
    assert unfiltered, "前提：清洗前也必须非空（否则断言无意义）"
    # 与“未清洗但已丢零贡献 token”的 OR 结果**逐项一致**（chunk_id 与 bm25 分都不变）。
    assert unfiltered == store.fts_search(cleaned, limit=50)
    candidates = recall_bm25(store, query, limit=50)
    assert [c.chunk_id for c in candidates] == [chunk_id for chunk_id, _ in unfiltered]


class _CountingStore:
    """``Store`` 透明代理：记录每次 ``fts_search`` 的 ``(串, limit, operator)``。

    TASK-020 性能回归用：只转发 ``fts_search``，其余属性一律透传（``recall_bm25`` 只用到它）。
    """

    def __init__(self, inner: Store) -> None:
        self._inner = inner
        self.calls: list[tuple[str, int, str]] = []

    def fts_search(self, segmented_query: str, limit: int = 50, *, operator: str = "or"):
        self.calls.append((segmented_query, limit, operator))
        return self._inner.fts_search(segmented_query, limit, operator=operator)  # type: ignore[arg-type]

    def __getattr__(self, name: str):
        return getattr(self._inner, name)
