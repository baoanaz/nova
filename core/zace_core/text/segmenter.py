"""CJK 预分词器：FTS5 unicode61 的中文修正（D-20 / D-45）。

背景（Module/02 §4.2-b）：unicode61 tokenizer 对连续中文不分词（整段成为一个 token），
中文查询直接失效。裁定方案：索引侧写入 FTS 前先分词、空格连接；查询侧用同一函数
预处理后再交给 ``Store.fts_search``。分词器与存储解耦（本模块不依赖 SQLite）。

实现约束：
- jieba 惰性初始化（首次调用才建词典），import 本模块无副作用、无日志噪音；
- 精确模式（默认 lcut），不做大小写/全半角归一化——归一化归 FTS unicode61。

**纯 ASCII 快速路径（TASK-114 / P0-1）**：``text.isascii()`` 时用一条正则复刻 jieba 的
ASCII 分词行为，不加载 jieba、不建词典：

```text
token = [A-Za-z0-9]+  或  任意单个非空白字符
```

选它而不是"直接返回原文"的原因：``segment`` 的产物不只喂 FTS——测试替身、诊断用的
``_content_tokens``、``filter_bm25_tokens`` 都按空格切 token。返回原文会让
``TokenService.refresh_token`` 变成一个 token，而 jieba 给的是
``TokenService . refresh _ token``，形状不同会改变这些消费方的行为（实测让
``service/tests/test_usage_api.py`` 的假 embedding 特征漂移）。本规则与 jieba 的 ASCII
形状**逐 token 相同**（实测 13 个样本 10 个完全相同；差异只在 jieba ``re_skip`` 会把
``3.14`` / ``42%`` / ``++`` 这类字面量并成一个 token，而这里逐字符拆开——两者经
unicode61 之后是完全相同的 token 流，不影响索引/查询）。

两条性质都在 ``core/tests/text/test_text_segmenter.py`` 里被固定：
① FTS token 流与 jieba 逐 token 相同（因此**不需要重建索引、召回不变**）；
② token 形状与 jieba 一致（除数字字面量分组）。

**非 ASCII 文本（含变音字母、非 CJK 文字）仍走 jieba**，与旧行为逐字节一致——这保证了
"文档含 CJK、查询纯 ASCII"（或反之）的混合场景两侧 token 空间不漂移，是 P0-1 的关键前提。

收益：langchain 的 chunk 里 >99.9% 不含 CJK，而 jieba 实测吞吐只有 0.32–0.54 MB/s，
跳过它是冷启动里最大的一块本地开销（handoff 实测 36.4s）。
"""

from __future__ import annotations

import logging
import re
from types import ModuleType

__all__ = ["segment"]

_jieba: ModuleType | None = None

#: ASCII 快速路径的 token 规则：字母数字串成词，其余非空白单字符各成一个 token。
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\S]")


def _load_jieba() -> ModuleType:
    global _jieba
    if _jieba is None:
        import jieba

        jieba.setLogLevel(logging.ERROR)  # 遮蔽 "Building prefix dict ..." 等日志
        _jieba = jieba
    return _jieba


def _jieba_segment(text: str) -> str:
    """jieba 参考实现（非 ASCII 路径）。单独成函数：等价性测试要拿它与快速路径对照。"""
    jieba = _load_jieba()
    return " ".join(token for token in jieba.cut(text) if token.strip())


def _ascii_segment(text: str) -> str:
    """纯 ASCII 的 jieba 等价分词（不加载 jieba，见模块 docstring）。"""
    return " ".join(_ASCII_TOKEN_RE.findall(text))


def segment(text: str) -> str:
    """jieba 精确模式分词并以空格连接；索引侧与查询侧必须调用本函数（D-45）。

    - 空白输入返回 ``""``（调用方按空查询处理）；
    - 纯 ASCII 输入走不加载 jieba 的等价快速路径（见模块 docstring）；
    - 输出 token 间恒为单空格，token 内不含空白；
    - 对未登录词/英文标识符 jieba 原样保留，FTS unicode61 再按自身规则切分。
    """
    if not text or not text.strip():
        return ""
    if text.isascii():
        return _ascii_segment(text)
    return _jieba_segment(text)
