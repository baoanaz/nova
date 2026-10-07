"""字面量通道（TASK-101 §A）：连续子串命中，tier 0。

**为什么需要它**（真实缺陷，非猜测）：查询里的长字面量是"配置键 / CLI 名 / 常量名 / 错误码"
这类问题的唯一指纹，但它们经 CJK 分词器切碎后进入 BM25 的 OR 匹配，会被高频词淹没——

```text
查询：cvi-agent-aibox 这个命令行入口最终指向哪个 python 函数？
jieba：cvi - agent - aibox 命令行 入口 main 函数 在 哪里
       ^^^ 三个 token 各自命中 215 / 691 / 87 个块（实测），pyproject.toml 掉到池内第 10、
       rerank 后第 21，被预算闸门挡在包外 → Agent 得出"仓库里没有 main 入口"的错误结论。
```

BM25 通道**不加短语查询**（会改变既有 OR 召回与已冻结的排序行为，R29/R30），因此单开本通道：
不切分、不调分词器，直接在切片正文里找连续子串（``Store.literal_search``），命中的候选按
tier 0 进池并吃 ``explicit literal`` 加分。

与 ``exact`` 通道的分工（不重叠）：

| 通道 | 输入 | 匹配对象 | 例 |
|---|---|---|---|
| ``exact`` | 反引号符号 / ``::``链 / 路径 | ``symbols`` 的 name/fqn | ``Runtime`` |
| ``literal`` | 反引号原文 / 长字面量 | ``chunks`` 正文的连续子串 | ``cvi-agent-aibox`` |

抽取规则（全部确定性，无词典、无关键词表）：

1. 反引号包裹的原文（含 ``cvi-agent-aibox``、``confirmation=REQUIRED`` 这类非标识符串）；
2. 含 ``-`` / ``=`` / ``/`` 等非标识符字符、且长度 ≥ :data:`MIN_LITERAL_CHARS` 的裸串
   （``AGENT_GRAPH_BACKEND`` 这类全大写长标识符也在此列——它是"唯一但被切碎"的典型）；
3. 已经在反引号里出现过的串不重复；

**不含**中文短语：中文靠 BM25 的 jieba 分词已能正确召回（实测中文查询无此缺陷），
本通道专治"被分词切碎就失效"的拉丁字面量。
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass

from nova_core.retrieval.fusion import (
    CHANNEL_LITERAL,
    TIER_EXPLICIT,
    TIER_SEED,
    make_candidate,
)
from nova_core.storage import Store
from nova_core.types import Candidate

__all__ = [
    "LITERAL_REASON_PREFIX",
    "MIN_LITERAL_CHARS",
    "REASON_LITERAL",
    "REASON_LITERAL_ROOT",
    "MIN_ROOT_CHARS",
    "LiteralPhrase",
    "extract_literal_phrases",
    "recall_literal",
]

#: 强字面量命中的原因/前缀（``_is_explicit`` 依赖它把字面量命中算作强证据）。
REASON_LITERAL = "explicit literal"
LITERAL_REASON_PREFIX = f"{REASON_LITERAL} "
#: 弱短语（标识符词根）命中的原因文本：**不计入** ``_is_explicit``（不冒充精确证据），
#: 只在 rerank 上给一个小额加分，避免"词根在符号表里到处都是"把整体分数抬上去。
REASON_LITERAL_ROOT = "literal root"

#: 强短语最小长度：以下交给 BM25（短串必然泛滥，且那是词法通道的职责）。
MIN_LITERAL_CHARS = 6
#: 弱短语（标识符词根）最小长度。为什么要它：``unicode61`` **不切 camelCase**——
#: 查询里的 ``capability`` 永远等不上正文/符号里的 ``CapabilityDefinition``（同一个 token），
#: 这类词根只有子串匹配能找到；而实测噪声可控的前提是**只匹配符号名**（正文里几乎所有
#: 调用点都含该词根）。见 :func:`recall_literal` 的强度差异说明。
MIN_ROOT_CHARS = 8
#: 弱短语单次最多带进的候选数。
DEFAULT_ROOT_LIMIT = 20
#: 单次召回的短语数上限（强短语优先、弱词根其次；单表扫描实测约 20ms/短语）。
MAX_LITERAL_PHRASES = 6
#: 单短语最多保留的候选数（命中次数降序，见 ``Store.literal_search``）。
DEFAULT_LITERAL_LIMIT = 30

_BACKTICK_RE = re.compile(r"`([^`]+)`")
#: 裸字面量：由字母数字与 ``-`` ``_`` ``.`` ``:`` ``/`` ``=`` 组成的连续串（≥ MIN_LITERAL_CHARS）。
_BARE_RE = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._:/=\-]{{{MIN_LITERAL_CHARS - 1},}}"
)
#: 含这些字符说明"一定是字面量而非自然语言词"（全大写长串是唯一的例外，见 ``_is_worth``）。
_STRUCTURAL_CHARS = "-=/:."


@dataclass(frozen=True, slots=True)
class LiteralPhrase:
    """一条字面量短语及其强度。

    ``strong=True``：反引号原文 / 含结构化字符的长串 / 全大写长串 —— 精确证据
    （tier 0，吃 ``explicit literal`` 的 +2.0 与 ``_is_explicit`` 判定）；
    ``strong=False``：标识符词根（``capability`` ⊂ ``CapabilityDefinition``）—— **只匹配
    符号名**的强召回（tier 1，rerank 小额加分，不计入 explicit）。
    """

    text: str
    strong: bool

    @property
    def reason(self) -> str:
        return REASON_LITERAL if self.strong else REASON_LITERAL_ROOT


def _tail_variants(head: str, tail: str) -> tuple[str, ...]:
    """``key=PREFIX.VALUE`` → 代码里可能出现的等价强串（只取 ``PREFIX.VALUE`` 这种点号链）。

    只为覆盖"查询写短、代码写全"这一种形态（``confirmation=REQUIRED`` vs 代码里的
    ``confirmation=ConfirmationPolicy.REQUIRED``）；不做模糊匹配——拼不出就返回空，宁漏不滥。
    """
    # 只接受**自身带结构**的写法（点号链/下划线链）：``REQUIRED`` 这种单个大写单词
    # 在代码里到处都是（``required=True`` 之外还有别的语义），当强字面量会泛滥（实测否决）。
    if _IDENTIFIER_TAIL_RE.fullmatch(tail) and ("." in tail or "_" in tail):
        return (tail,)
    return ()


#: 形如 ``ConfirmationPolicy.REQUIRED`` 的点号链（可带下划线/数字）。
_IDENTIFIER_TAIL_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
def _is_worth(token: str) -> bool:
    """强字面量的判据：含结构化字符（``-=/:``），或全大写/纯数字的长串。"""
    if len(token) < MIN_LITERAL_CHARS:
        return False
    if any(char in token for char in _STRUCTURAL_CHARS):
        return True
    # 全大写长标识符（AGENT_GRAPH_BACKEND / READONLY）：分词后仍是一个 token，但常被
    # jieba 与 unicode61 二次切分（下划线、大小写边界），字面量命中比词法命中可靠。
    return token.isupper() or token.isdigit()


def extract_literal_phrases(
    query: str,
    *,
    limit: int = MAX_LITERAL_PHRASES,
    covered: Collection[str] = (),
) -> tuple[LiteralPhrase, ...]:
    """查询 → 值得走字面量检索的短语（按出现顺序去重，最多 ``limit`` 个）。

    优先级：反引号/强串在前（它们更可能是用户显式点名的目标），弱词根在后——
    超出 ``limit`` 时先丢弃弱词根。

    ``covered``：**已被符号通道覆盖**的 token（``parse_explicit`` / ``extract_inferred`` 的结果）。
    弱短语若在 ``covered`` 里则丢弃：那是符号通道的活儿，字面量通道重复一遍只会
    把**同名但无意义的落点**（实测：``MainStream`` 在生成的 ``*_pb2_grpc.py`` 存根里同样出现）
    也抬进高分区，反而把真实现压下去。字面量通道只治"符号通道表达不了"的东西。
    """
    if not query or not query.strip():
        return ()
    covered_lower = {token.lower() for token in covered}
    strong: list[str] = []
    weak: list[str] = []
    seen: set[str] = set()

    def _add(raw: str, *, is_strong: bool = True) -> None:
        token = raw.strip()
        minimum = MIN_LITERAL_CHARS if is_strong else MIN_ROOT_CHARS
        if len(token) < minimum or token in seen:
            return
        seen.add(token)
        (strong if is_strong else weak).append(token)

    for match in _BACKTICK_RE.finditer(query):
        _add(match.group(1))
    for match in _BARE_RE.finditer(query):
        token = match.group(0)
        if _is_worth(token):
            _add(token)
        elif (
            len(token) >= MIN_ROOT_CHARS
            and token.isascii()
            and token.lower() not in covered_lower
        ):
            _add(token, is_strong=False)
            # ``key=value`` 形式：代码里常常是 ``key=PREFIX.VALUE``（实测
            # ``confirmation=REQUIRED`` → ``confirmation=ConfirmationPolicy.REQUIRED``），
            # 整串字面量因此零命中。补上 ``PREFIX.VALUE`` 的去前缀写法作为**另一个强短语**
            # （仍是精确串，不是词根），命中它同样说明"这就是那条声明"。
            head, separator, tail = token.partition("=")
            if separator and tail and head:
                for name in _tail_variants(head, tail):
                    _add(name)
    ordered = [LiteralPhrase(text=token, strong=True) for token in strong]
    ordered.extend(LiteralPhrase(text=token, strong=False) for token in weak)
    return tuple(ordered[:limit])


def recall_literal(
    store: Store,
    phrases: tuple[LiteralPhrase, ...] | list[LiteralPhrase],
    *,
    limit: int = DEFAULT_LITERAL_LIMIT,
) -> list[Candidate]:
    """字面量短语 → 候选（强短语 tier 0 / 弱词根 tier 1；命中切片已按"次数 + 短块"排序）。

    与其它通道一致：返回顺序即 rank（1-based），原因文本写进 ``reasons`` 供可解释性。
    命中次数只进原因文本，不进 RRF（D-16：融合只用排名）。
    """
    candidates: list[Candidate] = []
    seen: set[str] = set()
    for phrase in phrases:
        if phrase.strong:
            found: list[tuple[str, str]] = [
                (chunk_id, f"{hits} hits")
                for chunk_id, hits in store.literal_search(phrase.text, limit=limit)
            ]
        else:
            found = [
                (chunk_id, "symbol name")
                for chunk_id in store.symbol_literal_search(
                    phrase.text, limit=DEFAULT_ROOT_LIMIT
                )
            ]
        for chunk_id, detail in found:
            if chunk_id in seen:
                continue
            seen.add(chunk_id)
            candidates.append(
                make_candidate(
                    chunk_id,
                    channel=CHANNEL_LITERAL,
                    rank=len(candidates) + 1,
                    tier=TIER_EXPLICIT if phrase.strong else TIER_SEED,
                    reason=f"{phrase.reason} {phrase.text} ({detail})",
                )
            )
            if len(candidates) >= limit:
                return candidates
    return candidates
