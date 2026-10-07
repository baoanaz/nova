"""TASK-010 Exact 双档判定与符号匹配（DoD：三形态 Explicit 判定 + Inferred 抽取）。"""

from __future__ import annotations

import pytest
from nova_core.retrieval import exact
from nova_core.retrieval.exact import (
    explicit_tokens,
    extract_inferred,
    parse_explicit,
    recall_explicit,
    recall_inferred,
)
from nova_core.retrieval.fusion import CHANNEL_EXACT, CHANNEL_INFERRED, TIER_EXPLICIT, TIER_SEED


def _kinds(query: str) -> dict[str, str]:
    return {token.text: token.kind for token in explicit_tokens(query)}


def test_explicit_three_forms() -> None:
    """DoD：反引号标识符 / 含扩展名路径 / :: 与 . 标识符链。"""
    assert _kinds("`refresh_token` 在哪里定义")["refresh_token"] == "symbol"
    assert _kinds("看看 src/auth/token_service.py")["src/auth/token_service.py"] == "path"
    assert _kinds("TokenService::refresh 做了什么")["TokenService::refresh"] == "symbol"
    assert _kinds("TokenStore.rotate 的调用方")["TokenStore.rotate"] == "symbol"


def test_explicit_backticked_path_is_path() -> None:
    assert _kinds("`src/auth/token_service.py` 里的逻辑") == {"src/auth/token_service.py": "path"}


def test_explicit_chain_is_not_also_a_path() -> None:
    """``TokenService.refresh`` 不能因为含点被判成路径。"""
    assert _kinds("TokenService.refresh") == {"TokenService.refresh": "symbol"}


def test_inferred_extraction_three_forms() -> None:
    tokens = extract_inferred("refreshToken / refresh_token / REFRESH_TOKEN 各是什么")
    assert tokens[:3] == ["refreshToken", "refresh_token", "REFRESH_TOKEN"]


def test_inferred_includes_pascal_case_but_not_plain_words() -> None:
    tokens = extract_inferred("TokenService 和 RefreshCache 是类，This 不是")
    assert "TokenService" in tokens
    assert "RefreshCache" in tokens
    assert "This" not in tokens


def test_explicit_spans_are_not_re_extracted_as_inferred() -> None:
    inferred = extract_inferred("`refresh_token` 与 TokenService::refresh 的关系")
    assert "refresh_token" not in inferred
    assert "TokenService" not in inferred
    assert "refresh" not in inferred


def test_parse_explicit_splits_symbols_and_paths() -> None:
    parsed = parse_explicit("`refresh_token` 在 src/auth/token_service.py 的 TokenService::refresh")
    assert parsed.symbols == ("refresh_token", "TokenService::refresh")
    assert parsed.paths == ("src/auth/token_service.py",)


def test_explicit_tokens_dedup_and_empty_input() -> None:
    assert explicit_tokens("") == []
    tokens = explicit_tokens("`refresh_token` 和 refresh_token")
    assert [t.text for t in tokens] == ["refresh_token"]


def test_recall_explicit_matches_fqn_and_marks_tier_zero(store, seed_file, sym) -> None:
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[
            sym("refresh", "TokenService.refresh", kind="method", start=45, end=82),
            sym("rotate", "TokenStore.rotate", kind="method", start=10, end=20),
        ],
    )
    candidates = recall_explicit(store, ("TokenService::refresh",))
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.chunk_id == "src/auth/token_service.py:TokenService.refresh:45"
    assert candidate.tier == TIER_EXPLICIT
    assert candidate.channel_ranks == {CHANNEL_EXACT: 1}
    assert any(exact.REASON_EXPLICIT_SYMBOL in reason for reason in candidate.reasons)


def test_recall_explicit_respects_limit(store, seed_file, sym) -> None:
    symbols = [sym(f"f{i}", f"f{i}", start=i + 1) for i in range(5)]
    seed_file(store, path="src/a.py", symbols=symbols)
    assert len(recall_explicit(store, tuple(f"f{i}" for i in range(5)), limit=3)) == 3


def test_recall_inferred_is_normal_seed(store, seed_file, sym) -> None:
    seed_file(store, path="src/cache.py", symbols=[sym("refreshCache", "refreshCache", start=5)])
    candidates = recall_inferred(store, ["refreshCache"])
    assert len(candidates) == 1
    assert candidates[0].tier == TIER_SEED
    assert candidates[0].channel_ranks == {CHANNEL_INFERRED: 1}
    assert any(exact.REASON_INFERRED in reason for reason in candidates[0].reasons)


def test_recall_explicit_ignores_paths_and_missing_symbols(store, seed_file, sym) -> None:
    seed_file(store, path="src/a.py", symbols=[sym("f", "f", start=1)])
    assert recall_explicit(store, ("src/a.py",)) == []
    assert recall_explicit(store, ("NotThere",)) == []


def test_symbol_spellings_cover_both_separators() -> None:
    assert exact._symbol_spellings("A.b") == ["A.b", "A::b"]
    assert exact._symbol_spellings("A::b") == ["A::b", "A.b"]
    assert exact._symbol_spellings("plain") == ["plain"]


@pytest.mark.parametrize(
    "token,expected",
    [
        ("a.py", True),
        ("a.PY", True),
        ("foo.xyz", False),
        ("plain", False),
        ("token_service", False),
    ],
)
def test_is_path_token(token: str, expected: bool) -> None:
    assert exact._is_path_token(token) is expected
