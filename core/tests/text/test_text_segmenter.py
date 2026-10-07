"""jieba 预分词器行为（D-45：索引/查询同一函数）。"""

from __future__ import annotations

import random
import re
import sqlite3

import pytest
from nova_core.text import segment
from nova_core.text.segmenter import _jieba_segment

#: 快速路径与 jieba **逐 token 相同**的 ASCII 样本（代码里最常见的形态）。
_ASCII_SHAPE_IDENTICAL = (
    "def f(x): return refresh_token  # comment",
    "class CapabilityDefinition(Base): pass",
    "hello, world! don't panic: it's fine",
    "TokenService.refresh_token",
    "import numpy as np",
    "self.x = self.y[0]",
    "path/to/file.py:12:5",
    "<html><body data-x='1'/></html>",
    "A1b2C3",
    "def ok() -> int:\n    return 1\n",
)

#: 形状不同（jieba 的 ``re_skip`` 会把 ``3.14`` / ``42%`` / ``++`` 并成一个 token，
#: 快速路径逐字符拆开）但 **FTS token 流相同**——unicode61 两边都只认字母数字。
_ASCII_SHAPE_DIFFERENT = (
    "AGENT_GRAPH_BACKEND=1; x.y.z=0.0.8",
    "a-b_c.d e++f &g#h %i",
    "3.14 42% 0x1F v1.2.3-rc.1",
)

_ASCII_SAMPLES = _ASCII_SHAPE_IDENTICAL + _ASCII_SHAPE_DIFFERENT

#: 非 ASCII 样本：快速路径必须**不**生效（否则会与 jieba 的 token 空间漂移）。
_NON_ASCII_SAMPLES = ("刷新令牌", "refresh_token 过期后如何刷新", "café résumé", "ключ")


def _fts_terms(text: str) -> list[tuple[str, int]]:
    """文本经 unicode61 之后的 ``(term, count)``（用 fts5vocab 直读索引内部）。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE doc USING fts5(body, tokenize='unicode61')")
        if text:
            conn.execute("INSERT INTO doc(body) VALUES (?)", (text,))
        conn.execute("CREATE VIRTUAL TABLE vocab USING fts5vocab(doc, 'row')")
        rows = conn.execute("SELECT term, cnt FROM vocab ORDER BY term, cnt")
        return [tuple(row) for row in rows]
    finally:
        conn.close()


def test_cjk_tokens_are_space_separated() -> None:
    result = segment("刷新令牌")
    assert result == "刷新 令牌"


def test_segmentation_is_idempotent_for_spaced_output() -> None:
    once = segment("刷新令牌的过期时间")
    assert segment(once) == once


def test_mixed_chinese_and_identifier() -> None:
    # jieba 默认按下划线切标识符（'refresh_token' → refresh/_/token）；
    # FTS unicode61 侧同样以下划线为分隔符，索引/查询两侧 token 空间一致。
    tokens = segment("refresh_token 过期后如何刷新").split()
    assert "refresh" in tokens and "token" in tokens
    assert "过期" in tokens
    assert "刷新" in tokens


def test_empty_inputs() -> None:
    assert segment("") == ""
    assert segment("   \n\t ") == ""


def test_no_blank_tokens() -> None:
    tokens = segment("def f():  # 刷新 令牌\n    return None\n").split()
    assert tokens and all(token.strip() for token in tokens)


@pytest.mark.parametrize("text", _ASCII_SHAPE_IDENTICAL)
def test_ascii_fast_path_is_token_identical_to_jieba(text: str) -> None:
    """P0-1：常见 ASCII 形态下快速路径与 jieba **逐 token 相同**（消费方无感）。"""
    assert segment(text) == _jieba_segment(text)


@pytest.mark.parametrize("text", _ASCII_SAMPLES)
def test_ascii_fast_path_is_token_equivalent_to_jieba(text: str) -> None:
    """P0-1 的正确性锚点：快速路径与 jieba 在 unicode61 下逐 token 相同。

    这是"P0-1 不是检索语义变更"的证明：FTS 的 token 空间不变 ⇒ 索引与查询行为不变、
    不需要重建索引、不会改变召回结果。任何后续改动破坏它都会在这里红。
    """
    assert _fts_terms(segment(text)) == _fts_terms(_jieba_segment(text))


@pytest.mark.parametrize("text", _ASCII_SHAPE_DIFFERENT)
def test_literal_grouping_differs_but_tokens_match(text: str) -> None:
    """相差只在字面量分组（``3.14`` vs ``3 . 14``）：字母数字 token 集合仍相同。"""
    assert re.findall(r"[A-Za-z0-9]+", segment(text)) == re.findall(
        r"[A-Za-z0-9]+", _jieba_segment(text)
    )


def test_ascii_fast_path_does_not_load_jieba(monkeypatch: pytest.MonkeyPatch) -> None:
    """快速路径不得触发 jieba 导入/建词典——这正是 P0-1 收益的来源。"""
    from nova_core.text import segmenter

    monkeypatch.setattr(segmenter, "_jieba", None)
    assert segment("def f(x): return 1") == "def f ( x ) : return 1"
    assert segmenter._jieba is None


@pytest.mark.parametrize("text", _NON_ASCII_SAMPLES)
def test_non_ascii_input_still_uses_jieba(text: str) -> None:
    """含变音字母/其它文字时仍逐字节等于 jieba 输出。"""
    assert segment(text) == _jieba_segment(text)


def test_mixed_text_keeps_jieba_for_the_whole_string() -> None:
    """混合文本整体走 jieba，ASCII 片段的 token 与快速路径一致（跨路径可命中）。"""
    mixed = "刷新 refresh_token 过期"
    tokens = segment(mixed).split()
    assert {"refresh", "token"} <= set(tokens)
    # 文档侧若是不含 CJK 的纯 ASCII，快速路径给出的 token 必须能与上面的查询对上。
    ascii_terms = {term for term, _ in _fts_terms(segment("return refresh_token"))}
    assert {"refresh", "token"} <= ascii_terms


_PUNCTUATED_ASCII_SAMPLES = (
    "TokenService.refresh_token — returns ‘token’…",
    "def f(x):  # → refresh_token\n    return 3.14\n",
    "3.14 42% 0x1F v1.2.3-rc.1 ± 2.0%",
    "a-b_c.d e++f &g#h %i — -- __ .. %% ++ ## &&",
    "hello\u00a0world\u2003again\u2028end\u0085next",
    "© 2026 • price €42 ™ ✓ ★ 😀",
    "（hello），world！「refresh_token」",
    "\u00a0\u2003\u2028\u0085",
    "—…±😀",
)


def _fts_token_stream(text: str) -> list[tuple[str, int]]:
    """instance 视图保留顺序、重复项和位置，避免词频相同掩盖 token 重排。"""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE doc USING fts5(body, tokenize='unicode61')")
        conn.execute("INSERT INTO doc(body) VALUES (?)", (text,))
        conn.execute("CREATE VIRTUAL TABLE vocab USING fts5vocab(doc, 'instance')")
        return list(conn.execute("SELECT term, offset FROM vocab ORDER BY offset"))
    finally:
        conn.close()


@pytest.mark.parametrize("text", _PUNCTUATED_ASCII_SAMPLES)
def test_punctuated_ascii_preserves_shape_and_fts_stream(text: str) -> None:
    reference = _jieba_segment(text)
    assert segment(text) == reference
    assert _fts_token_stream(segment(text)) == _fts_token_stream(reference)


@pytest.mark.parametrize("text", _PUNCTUATED_ASCII_SAMPLES)
def test_punctuated_ascii_does_not_load_jieba(
    text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nova_core.text import segmenter

    reference = _jieba_segment(text)

    def forbidden_load() -> None:
        pytest.fail("eligible punctuation-only text must bypass jieba")

    monkeypatch.setattr(segmenter, "_jieba", None)
    monkeypatch.setattr(segmenter, "_load_jieba", forbidden_load)
    assert segment(text) == reference
    assert segmenter._jieba is None


@pytest.mark.parametrize(
    "text",
    _NON_ASCII_SAMPLES
    + (
        "hello — 中文",
        "cafe\u0301 — resume",
        "hello — 日本語 カタカナ 한글 العربية",
        "value — １２３ ١٢٣ ² Ⅳ",
        "emoji 👩\u200d💻 variant ✓\ufe0f",
        "hidden\u200btext — value",
        "surrogate \ud800 — value",
    )
    + tuple(
        f"{prefix}{word}{suffix} — language"
        for word in ("AT&T", "C#", "c#", "C++", "c++")
        for prefix, suffix in (("", ""), ("prefix", "suffix"))
    ),
)
def test_new_fast_path_falls_back_for_language_and_dictionary_words(
    text: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from nova_core.text import segmenter

    calls = []

    def reference(value: str) -> str:
        calls.append(value)
        return _jieba_segment(value)

    monkeypatch.setattr(segmenter, "_jieba_segment", reference)
    assert segment(text) == _jieba_segment(text)
    assert calls == [text]


def test_punctuated_ascii_generated_differential() -> None:
    """固定种子的边界组合：字面量、ASCII 控制符和 Unicode 分隔符。"""
    rng = random.Random(114)
    alphabet = "abcXYZ019.+#&_%-/=() \t\n\r\x00\x1c—…±😀\u00a0\u2003\u0085"
    for _ in range(300):
        text = "—" + "".join(rng.choices(alphabet, k=rng.randrange(1, 150)))
        reference = _jieba_segment(text)
        actual = segment(text)
        assert actual == reference, repr(text)
        assert _fts_token_stream(actual) == _fts_token_stream(reference), repr(text)


@pytest.mark.parametrize("text", _PUNCTUATED_ASCII_SAMPLES)
def test_punctuated_ascii_fts_phrase_matches_reference(text: str) -> None:
    """旧索引/新查询和新索引/旧查询的 phrase 命中保持一致。"""
    old = _jieba_segment(text)
    new = segment(text)
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE VIRTUAL TABLE doc USING fts5(body, tokenize='unicode61')")
        conn.executemany("INSERT INTO doc(body) VALUES (?)", [(old,), (new,)])
        for query_text in (old, new):
            terms = [term for term, _ in _fts_token_stream(query_text)]
            if not terms:
                assert _fts_token_stream(old) == _fts_token_stream(new) == []
                continue
            query = '"' + " ".join(terms).replace('"', '""') + '"'
            rows = conn.execute("SELECT rowid FROM doc WHERE doc MATCH ?", (query,))
            assert list(rows) == [(1,), (2,)]
    finally:
        conn.close()
