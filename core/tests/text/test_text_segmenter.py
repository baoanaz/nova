"""jieba 预分词器行为（D-45：索引/查询同一函数）。"""

from __future__ import annotations

import re
import sqlite3

import pytest
from zace_core.text import segment
from zace_core.text.segmenter import _jieba_segment

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
    from zace_core.text import segmenter

    monkeypatch.setattr(segmenter, "_jieba", None)
    assert segment("def f(x): return 1") == "def f ( x ) : return 1"
    assert segmenter._jieba is None


@pytest.mark.parametrize("text", _NON_ASCII_SAMPLES)
def test_non_ascii_input_still_uses_jieba(text: str) -> None:
    """分派条件必须只放行纯 ASCII——含变音字母/其它文字时逐字节等于 jieba 输出。"""
    assert segment(text) == _jieba_segment(text)


def test_mixed_text_keeps_jieba_for_the_whole_string() -> None:
    """混合文本整体走 jieba，ASCII 片段的 token 与快速路径一致（跨路径可命中）。"""
    mixed = "刷新 refresh_token 过期"
    tokens = segment(mixed).split()
    assert {"refresh", "token"} <= set(tokens)
    # 文档侧若是不含 CJK 的纯 ASCII，快速路径给出的 token 必须能与上面的查询对上。
    ascii_terms = {term for term, _ in _fts_terms(segment("return refresh_token"))}
    assert {"refresh", "token"} <= ascii_terms
