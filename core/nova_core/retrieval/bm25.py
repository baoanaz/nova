"""BM25 通道（TASK-010）：FTS5 词法召回 + CJK 双侧同分词。

设计依据：``docs/design/Module/02-检索策略.md`` §4.2-b（D-20 / D-45）：

- 配额 top 50；查询串必须经 :func:`nova_core.text.segment`（与索引侧同一分词器）；
- 多 token 采用 **OR 连接**（R11，2026-09-10 集成期裁定）：中文自然语言查询分词后
token 多，隐式 AND 会恒零命中；高精度需求由 ``Store.fts_search(operator="and")`` 显式选择；
- FTS 返回**原始 bm25 分（越小越相关，列权重见 `FTS_COLUMN_WEIGHTS`）并已按它升序**——
本模块把它转成 1-based 排名；
- generated 文件不在此排除（rerank 降权是 TASK-011 的事，D-02/Module/02 §4.2-b）。

TASK-020 §D（2026-09-10，编排者收窄为「零成本清理」）：进入 MATCH 前只做**纯函数级**噪声清洗
——纯标点/空白 token（如 `？`）丢弃，不引入任何 SQL。

- **不做逐 token 的 DF 探测**：``fts_search`` 的唯一生产调用方就是本模块，且恒用
  ``operator="or"``；OR 语义下 DF=0（库外）token 对结果**零影响**（R20 实测：含/不含 `？`
  的 OR 结果逐项相同），探测只带来 1+N 条 SQL（实测约 7.4ms/查询）而无收益。
- **不做 IDF 加权重排/停用词表**：IDF 重排已实测否决（R20/R24：Σ IDF 后目标 rank 797/148，
  均不达 ≤10；虚词 IDF 高于意图词 ⇒ 稀有度在本语料不可靠），须先在 golden set 建基线。

约定（**调用方责任**）：``operator="and"`` 路径不由本模块清洗——该路径目前无生产调用方；
未来启用时调用方需自备 DF=0 清洗（可切分但库外与标点不同，**会使 AND 恒空**，R20 实测），
:func:`filter_bm25_tokens` 不接 ``store``、不做库内探测，故不会替调用方做这件事。
"""

from __future__ import annotations

from collections.abc import Sequence

from nova_core.retrieval.fusion import CHANNEL_BM25, TIER_SEED, make_candidate
from nova_core.storage import Store
from nova_core.text import segment
from nova_core.types import Candidate

__all__ = [
    "REASON_BM25",
    "bm25_query_text",
    "filter_bm25_tokens",
    "is_noise_token",
    "recall_bm25",
]

REASON_BM25 = "bm25"


def bm25_query_text(query: str) -> str:
    """BM25 侧查询归一化（L1 实现细节，不改分词器）：去反引号、``::`` 与 ``::`` 残留替换为空格。

    理由：jieba 会把 `` ` `` 与 ``:`` 切成独立 token，而正文里没有这些 token。OR 语义下
    噪声 token 虽不再让整条查询失配，却会**无差别扩大候选**（正文里的 ``:`` 到处都是），
    显式符号链查询（``TokenService::refresh``）尤其明显。其它通道与渲染一律用原文，
    本归一化只作用于 BM25 的 MATCH 串。
    """
    return query.replace("`", " ").replace("::", " ")


def is_noise_token(token: str) -> bool:
    """纯标点/空白 token 判定（TASK-020 §D.1）：不含任何字母、数字或 CJK 字符。

    ``str.isalnum()`` 对 CJK 字符为真（Unicode 归类为字母），因此 `？` `：` `,` `.` `—`
    以及 ``::`` 拆出的 `:` 一律判为噪声——正是 jieba 把标点切成独立 token 的产物。
    判据只看字符类别、不看语义，因此不是停用词表/关键词表（R4 的「少规则」纪律不违反）。
    """
    return not any(ch.isalnum() for ch in token)


def filter_bm25_tokens(tokens: Sequence[str]) -> list[str]:
    """BM25 查询侧 token 清洗（TASK-020 §D.1）：丢弃空串与纯标点/空白 token。

    **纯函数、零 SQL**：签名不接 ``store``，因此不可能做逐 token 的 DF 探测（§D.3）。
    DF=0 的库外 token 在 OR 路径上对召回零影响（R20 实测），无需也不应在此探测；
    ``operator="and"`` 的 DF 清洗由调用方自备（见模块 docstring 的约定）。
    """
    return [token for token in tokens if token and not is_noise_token(token)]


def recall_bm25(store: Store, query: str, *, limit: int = 50) -> list[Candidate]:
    """BM25 召回：``segment(bm25_query_text(query))`` → 噪声清洗 → ``Store.fts_search``
    → 排名候选（tier 1）。

    清洗只作用于本通道的 MATCH 串（TASK-020 §D）：**单次调用恒为 1 条 ``fts_search`` SQL**
    （不随 token 数增长），且**不改变 OR 召回结果**（R20 实测：DF=0 token 零影响）。
    空查询/全空白分词结果/全 token 被清洗 → 空列表（不抛异常）。FTS 原始分仅写进 ``reasons``
    供解释，不进 RRF（D-16：融合层只用排名，不用分数）。
    """
    segmented = segment(bm25_query_text(query))
    if not segmented.strip():
        return []
    tokens = filter_bm25_tokens(segmented.split())
    if not tokens:
        return []
    hits = store.fts_search(" ".join(tokens), limit=limit)
    return [
        make_candidate(
            chunk_id,
            channel=CHANNEL_BM25,
            rank=rank,
            tier=TIER_SEED,
            reason=f"{REASON_BM25} {raw_score:.4f}",
        )
        for rank, (chunk_id, raw_score) in enumerate(hits, start=1)
    ]
