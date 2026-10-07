"""确定性 Evidence Rerank（TASK-011 §B，Module/02 §4.5，D-16：V1 内置、无模型、零延迟）。

15 条特征逐项落地（分值为初始值，TASK-015 校准**只改默认值不改特征结构**）：

| 特征 | 分值 | 信号来源 |
|---|---|---|
| Explicit symbol/path 命中 | +2.0 | Exact-Explicit（§4.2-a） |
| **字面量命中**（反引号/含 ``-=/:`` 的长串） | +1.0 | Literal 通道（TASK-101 §A） |
| **标识符词根命中**（仅符号名） | +0.4 | Literal 通道弱短语 |
| query 符号名与 chunk 符号名等值 | +1.0 | Exact-Inferred |
| 3 通道共识 | +0.5 | candidate.channel_ranks |
| **向量 rank ≤ 8**（语义相关） | +1.5 | candidate.channel_ranks（TASK-105） |
| **向量 rank 9-10**（语义较近） | +0.7 | candidate.channel_ranks（TASK-105） |
| 与 top-1 种子图连通（1 跳） | +0.5 | expand 的 ``graph-expanded from`` 记录 |
| doctype ∈ HIGH_VALUE_DOCTYPES | +0.8 | classify_doctype（索引侧同一函数） |
| 入口点 / 被导出符号 | +0.2 | symbols.is_exported + 无内部调用者 |
| generated 文件 | −1.0 | signals.generated_paths（见下） |
| test fixture（常态抑制，测试仅作参考） | −1.5 | candidate.kind=test；查询明示测试意图时不降 |
| stale spec 引用 | −0.8 | spec_references.stale |
| fallback_block | −0.5 | chunks.symbol_kind |
| 图距离 2 跳 | −0.3 | candidate.graph_depth |
| 合成边（provenance=synthesized）扩展 | −0.2 | edges.provenance |

基准分与量级：``score = rrf_score * RRF_BASE_SCALE + Σ特征``。
``rrf_score = Σ 1/(60+rank) ∈ (0, 0.033]``，与特征表（±0.2..2.0）差两个数量级；不缩放的话
特征表实际失效（单个 +2.0 恒压过任何 RRF 差异），Module/02 §4.6 的反例（"Explicit 命中无关 UI
模块 vs 三通道共识的强相关符号 → 共识者胜"）也无法成立。

TASK-105 修正了两个实测确认的缺陷（三仓 57 正例 + 3 负例，零 embedding 成本的离线回放）：

1. **特征表缺"语义相关性"**：正确答案常在 vector rank 1-3，却因只命中单通道而被
   "多通道沾边"压到 17-58（RRF 的 ``Σ1/(60+rank)`` 实际在奖励通道数量而非相关性）。
   故新增 ``vector_rank_top`` / ``vector_rank_near`` 两档。
2. **``RRF_BASE_SCALE`` 过大使特征失效**：100.0 下 base 分区间为 1.6-4.9，任何特征
   （±0.2..2.0）都压不过 base 差；降到 ``25.0`` 后特征重新具备区分度。
   隔离实验证实：只降 scale 不加向量特征反而变差（见任务卡执行记录），增益来自向量特征。

数据来源缺口（详见任务卡"未决问题"）：``files.generated`` 目前既无 Store 读 API、W1 写入端
也恒写 0，因此 ``is_generated_path`` 用 Module/01 §4.2 的**文件名约定**做临时代理；
``files.generated`` 可读后应切换为读取索引信号（届时本函数只改 ``collect_signals`` 一处）。
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from nova_core.parsing.markdown import classify_doctype
from nova_core.pipeline.generated import is_generated_path as _is_generated_path
from nova_core.retrieval.exact import extract_inferred, parse_explicit
from nova_core.retrieval.expand import GRAPH_REASON_PREFIX, SYNTHESIZED_REASON
from nova_core.retrieval.fusion import (
    CHANNEL_VECTOR,
    KIND_FALLBACK,
    KIND_SPEC,
    KIND_TEST,
    REASON_EXPLICIT_PATH,
    is_test_path,
)
from nova_core.retrieval.literal import REASON_LITERAL, REASON_LITERAL_ROOT
from nova_core.storage import Store
from nova_core.types import Candidate

__all__ = [
    "FEATURE_NAMES",
    "HIGH_VALUE_DOCTYPES",
    "RRF_BASE_SCALE",
    "LITERAL_ROOT_WEIGHT_ENV",
    "LITERAL_WEIGHT_ENV",
    "RerankSignals",
    "RerankWeights",
    "collect_signals",
    "features",
    "is_generated_path",
    "is_test_intent",
    "rerank",
    "weights_from_env",
    "with_weights",
]

#: 基准分缩放（见模块 docstring“基准分与量级”）。
#: TASK-105 实测：100.0 使 base 分区间（1.6-4.9）远大于特征表（±0.2-2.0），
#: 特征实际失效；降到 25.0 让特征重新具备区分度。
RRF_BASE_SCALE = 25.0

#: 向量语义特征的覆盖窗口：top-8 拿满分档，9-10 拿次档（TASK-105 实测定档）。
VECTOR_TOP_K = 8
VECTOR_NEAR_K = 10

#: TASK-101 新增权重的环境变量覆盖名（A/B 用；见 :func:`weights_from_env`）。
LITERAL_WEIGHT_ENV = "NOVA_W_LITERAL"
LITERAL_ROOT_WEIGHT_ENV = "NOVA_W_LITERAL_ROOT"

#: rerank 加分的 doctype 白名单（D-42 高信息密度文档）。
HIGH_VALUE_DOCTYPES = frozenset({"agent-instructions", "design", "adr", "readme", "api"})

FEATURE_EXPLICIT = "explicit symbol/path hit"
FEATURE_LITERAL = "literal hit"
FEATURE_LITERAL_ROOT = "literal root name hit"
FEATURE_SYMBOL_MATCH = "query symbol == chunk symbol"
FEATURE_CONSENSUS3 = "3-channel consensus"
FEATURE_VECTOR_TOP = "vector top-8 (semantic relevance)"
FEATURE_VECTOR_NEAR = "vector top-10 (semantic relevance)"
FEATURE_GRAPH_1HOP = "graph connected to top-1 seed (1 hop)"
FEATURE_DOCTYPE = "high-value doctype"
FEATURE_ENTRY_POINT = "entry point / exported symbol"
FEATURE_GENERATED = "generated file"
#: **已弃用符号**（TASK-MCP-BUDGET）：该 chunk 的正文带 deprecation 装饰器
#: （Python ``@deprecated(...)`` / Java ``@Deprecated`` 等）。
#:
#: 为什么是**符号级**而不是文件级：同一符号在跃包与兼容层各有一份时，只有后者被
#: 装饰器标注（实测 langchain ``init_chat_model``：classic 有 ``@deprecated(``、v1 无）；
#: 而“文件导入了 deprecation 工具”会误伤**跃包本身**（``langchain_core`` 就是提供者，
#: 它导入 ``_api.deprecated`` 再用到别处，导致真实实现 ``chat_models.py`` 被误降——实测坑到）。
FEATURE_DEPRECATED_PATH = "deprecated symbol"
FEATURE_TEST_FIXTURE = "test fixture (suppressed; tests are reference only)"
FEATURE_STALE_SPEC = "stale spec reference"
FEATURE_FALLBACK = "fallback_block"
FEATURE_GRAPH_2HOP = "graph distance 2 hops"
FEATURE_SYNTHESIZED = "synthesized edge"

#: 特征名（供测试与 03 的可解释性断言引用）。
FEATURE_NAMES = (
    FEATURE_EXPLICIT,
    FEATURE_LITERAL,
    FEATURE_LITERAL_ROOT,
    FEATURE_SYMBOL_MATCH,
    FEATURE_CONSENSUS3,
    FEATURE_VECTOR_TOP,
    FEATURE_VECTOR_NEAR,
    FEATURE_GRAPH_1HOP,
    FEATURE_DOCTYPE,
    FEATURE_ENTRY_POINT,
    FEATURE_GENERATED,
    FEATURE_DEPRECATED_PATH,
    FEATURE_TEST_FIXTURE,
    FEATURE_STALE_SPEC,
    FEATURE_FALLBACK,
    FEATURE_GRAPH_2HOP,
    FEATURE_SYNTHESIZED,
)

#: "测试意图查询" 的确定性词表（V1 口径，见任务卡"执行记录"）。
_TEST_INTENT_WORDS = (
    "test",
    "tests",
    "pytest",
    "unittest",
    "fixture",
    "测试",
    "单测",
    "用例",
)

#: deprecation **装饰器**的匹配子串（TASK-MCP-BUDGET）：符号级信号。
#:
#: 大小写不敏感子串，覆盖各语言常见写法：Python ``@deprecated(...)``；
#: Java ``@Deprecated``；TS ``@deprecated``。
#: 带 ``@`` 前缀是为了只命中“被标注的符号”，而不是命中注释/导入行里的普通提及。
DEPRECATED_MARKER = "@deprecat"

_GENERATED_NAME_MARKERS = ("_generated.", "autogen", "autogenerated")
_GENERATED_SUFFIXES = (".pb.cc", ".pb.h", ".pb.cpp", ".pb2.py", ".designer.cs", ".g.cs")


def _stored_generated_paths(store: Store) -> frozenset[str]:
    """读 ``files.generated`` 列（P2-5）；Store 不支持时返回空集。

    为什么探测而不是硬调：``collect_signals`` 的测试与部分调用方传的是只实现读路径的
    轻量 Store 替身；硬调会让它们在升级前就崩，而空集的语义（不额外标记）是安全的。
    """
    reader = getattr(store, "generated_files", None)
    if not callable(reader):
        return frozenset()
    return reader()


def is_generated_path(path: str) -> bool:
    """generated 文件的**文件名约定**判定（Module/01 §4.2 的 V1 规则）。

    TASK-REVIEW-RUNTIME P2-5：真实判定已下沉到索引期（``pipeline.generated`` 写
    ``files.generated`` 列，含内容 banner）。本函数保留为无 ``Store`` 时的降级路径，
    实现转发到同一份规则，不在两处各写一套。
    """
    return _is_generated_path(path)


def is_test_intent(query: str) -> bool:
    """查询是否带测试意图（V1 确定性规则：命中测试词表）。"""
    lowered = query.lower()
    return any(word in lowered for word in _TEST_INTENT_WORDS)


@dataclass(frozen=True, slots=True)
class RerankWeights:
    """12 条特征的分值（字段与 Module/02 §4.5 特征表一一对应）。"""

    explicit_hit: float = 2.0
    #: 字面量命中（TASK-101 §A）。取 1.0 而非 Explicit 的 2.0，是**实测结果**（两套 golden A/B，
    #: 见 benches/results 的 TASK-101 报告）：2.0 会把无关的短配置块顶到 top-1
    #: （nova golden MRR 0.421→0.395），1.0 既保住召回又不再盖过三通道共识（MRR 0.439）。
    literal_hit: float = 1.0
    #: 标识符词根命中（弱短语，仅符号名）：强召回信号，但远轻于点名——词根在符号表里
    #: 可能有十几处，给大分等于让"名字里含这个词"主导排序。
    literal_root: float = 0.4
    symbol_match: float = 1.0
    consensus3: float = 0.5
    #: 向量 rank 语义特征（TASK-105）：命中文档在语义通道的排名就是它的相关度证据。
    #: 原特征表没有“语义相关性”这一项，导致“多通道沾边”恒压过“语义最相关”（实测：正确答案
    #: 常在 vector rank 1-3，却因只命中单通道而掉到 17-58）。拆成两档，避免硬阈值跳变。
    vector_rank_top: float = 1.5
    vector_rank_near: float = 0.7
    graph_1hop_top1: float = 0.5
    doctype_high_value: float = 0.8
    entry_point: float = 0.2
    generated: float = -1.0
    #: 测试夹具降权（TASK-108：从“仅测试意图时生效”改为**常态抑制**）。
    #:
    #: 为什么改：原口径是 ``kind=test and not test_intent`` 才扣分，于是无测试意图的查询
    #: 不会降测试。实测反例（S3：问 Gateway 的幂等/验证/重试实现）：
    #: 查询词恰好是测试函数名里的高频词（``test_read_only_retry_reuses_identity_and_
    #: idempotency_key``），BM25 把 20 条测试顶进包，而真正的实现
    #: （``capability/gateway.py``）**一条都没进**——测试调用的参数名往往比实现的
    #: 私有方法名更贴近自然语言，这是结构性劣势。
    #: 现在常态降权（测试仅作参考）；查询**明示**测试意图时（“测试/单测/fixture/用例…”）
    #: 不降，因为那时用户要的就是测试。
    test_fixture: float = -1.5
    stale_spec_ref: float = -0.8
    fallback_block: float = -0.5
    graph_2hop: float = -0.3
    synthesized_edge: float = -0.2
    #: **已弃用路径降权**（TASK-MCP-BUDGET）。
    #:
    #: 实测背景（langchain，2026-09-16）：同一个符号在**活跃包**（``langchain_v1``）
    #: 与**弃用兼容层**（``langchain_classic``）各有一份实现，两者语义几乎相同，
    #: 分数只差 0.006（3.4939 vs 3.4878）——兼容层因 ``inferred`` 通道排名靠前而稳定胜出，
    #: 于是 Agent 拿到已弃用实现做结论（真实用例：init_chat_model 的 provider 推断，
    #: 评价指出“把 active v1 与已弃用的 classic 混在一起”）。
    #:
    #: 为什么取 -0.35：只差 0.006 的竞品需要被隔开，但不能把兼容层完全压死
    #: （它仍是真实代码，用户明确问“兼容层怎么做”时应当能看到）。-0.35 大于实测分差
    #: 一个量级，又远小于两档之间的典型间隔（约 1.0），不会跨档误伤。
    deprecated_path: float = -0.35


@dataclass(frozen=True, slots=True)
class RerankSignals:
    """rerank 的候选外信号（由 ``collect_signals`` 从索引与查询收集）。"""

    query_symbols: frozenset[str] = frozenset()
    explicit_chunk_ids: frozenset[str] = frozenset()
    top1_seed_chunk_id: str | None = None
    exported_chunk_ids: frozenset[str] = frozenset()
    generated_paths: frozenset[str] = frozenset()
    deprecated_chunk_ids: frozenset[str] = frozenset()
    stale_chunk_ids: frozenset[str] = frozenset()
    test_intent: bool = False
    doctype_by_chunk: Mapping[str, str] = field(default_factory=dict)


def _symbol_names(symbol: str) -> set[str]:
    """符号的等价拼写集合（``A.b`` / ``A::b`` 视为同一符号）。"""
    normalized = symbol.replace("::", ".")
    return {symbol, normalized, normalized.rsplit(".", 1)[-1], symbol.replace(".", "::")}


def _explicit_hit(candidate: Candidate) -> bool:
    if candidate.tier == 0 and "exact" in candidate.channel_ranks:
        return True
    if "exact" in candidate.channel_ranks:
        return True
    return REASON_EXPLICIT_PATH in candidate.reasons


def _literal_hit(candidate: Candidate) -> bool:
    """强字面量命中（来自 Literal 通道的 tier 0 短语）。"""
    return any(reason.startswith(REASON_LITERAL + " ") for reason in candidate.reasons)


def _literal_root_hit(candidate: Candidate) -> bool:
    """标识符词根命中（弱短语；**不是**精确证据，见 ``literal.py``）。"""
    return any(reason.startswith(REASON_LITERAL_ROOT + " ") for reason in candidate.reasons)


def _graph_parent(candidate: Candidate) -> str | None:
    for reason in candidate.reasons:
        if reason.startswith(GRAPH_REASON_PREFIX):
            return reason[len(GRAPH_REASON_PREFIX) :]
    return None


def _doctype(candidate: Candidate, signals: RerankSignals) -> str | None:
    known = signals.doctype_by_chunk.get(candidate.chunk_id)
    if known is not None:
        return known
    if candidate.kind == KIND_SPEC and candidate.path:
        return classify_doctype(candidate.path)
    return None


def features(
    candidate: Candidate,
    signals: RerankSignals,
    weights: RerankWeights,
) -> list[tuple[str, float]]:
    """逐条判定 12 个特征，返回命中的 ``(特征名, 分值)`` 列表（未命中不返回）。"""
    hits: list[tuple[str, float]] = []

    def hit(name: str, value: float, condition: bool) -> None:
        if condition and value != 0.0:
            hits.append((name, value))

    hit(FEATURE_EXPLICIT, weights.explicit_hit, _explicit_hit(candidate))
    hit(FEATURE_LITERAL, weights.literal_hit, _literal_hit(candidate))
    hit(FEATURE_LITERAL_ROOT, weights.literal_root, _literal_root_hit(candidate))
    symbol = candidate.symbol_fqn
    if symbol and signals.query_symbols:
        spellings = _symbol_names(symbol)
        hit(
            FEATURE_SYMBOL_MATCH,
            weights.symbol_match,
            any(spelling in signals.query_symbols for spelling in spellings),
        )

    hit(FEATURE_CONSENSUS3, weights.consensus3, len(candidate.channel_ranks) >= 3)
    # 向量 rank 语义特征：排名越靠前说明语义越贴近问题。分两档且仅覆盖 top-10——
    # 向量通道自身召回上限是 50，再往后排名的区分度不足以作为证据。
    vector_rank = candidate.channel_ranks.get(CHANNEL_VECTOR)
    if vector_rank is not None:
        hit(FEATURE_VECTOR_TOP, weights.vector_rank_top, vector_rank <= VECTOR_TOP_K)
        hit(
            FEATURE_VECTOR_NEAR,
            weights.vector_rank_near,
            VECTOR_TOP_K < vector_rank <= VECTOR_NEAR_K,
        )

    parent = _graph_parent(candidate)
    hit(
        FEATURE_GRAPH_1HOP,
        weights.graph_1hop_top1,
        parent is not None
        and candidate.graph_depth == 1
        and signals.top1_seed_chunk_id is not None
        and parent == signals.top1_seed_chunk_id,
    )

    doctype = _doctype(candidate, signals)
    hit(
        FEATURE_DOCTYPE,
        weights.doctype_high_value,
        doctype in HIGH_VALUE_DOCTYPES if doctype is not None else False,
    )
    hit(FEATURE_ENTRY_POINT, weights.entry_point, candidate.chunk_id in signals.exported_chunk_ids)
    hit(
        FEATURE_GENERATED,
        weights.generated,
        bool(candidate.path) and candidate.path in signals.generated_paths,
    )
    hit(
        FEATURE_DEPRECATED_PATH,
        weights.deprecated_path,
        candidate.chunk_id in signals.deprecated_chunk_ids,
    )
    hit(
        FEATURE_TEST_FIXTURE,
        # TASK-108：常态抑制（仅查询明示测试意图时不降）——测试仅作参考，
        # 实现证据优先。取值 -1.5 而非 -0.5：实测 -0.5 不足以把“同名测试”拉下
        # 实现候选（S3 里测试以 1.84 分位居前列，而实现候选普遍在 1.2-1.4）。
        0.0 if signals.test_intent else weights.test_fixture,
        candidate.kind == KIND_TEST,
    )
    hit(FEATURE_STALE_SPEC, weights.stale_spec_ref, candidate.chunk_id in signals.stale_chunk_ids)
    hit(FEATURE_FALLBACK, weights.fallback_block, candidate.kind == KIND_FALLBACK)
    hit(FEATURE_GRAPH_2HOP, weights.graph_2hop, candidate.graph_depth >= 2)
    hit(FEATURE_SYNTHESIZED, weights.synthesized_edge, SYNTHESIZED_REASON in candidate.reasons)
    return hits


def rerank(
    candidates: Sequence[Candidate],
    signals: RerankSignals | None = None,
    weights: RerankWeights | None = None,
) -> list[Candidate]:
    """就地写入 ``score`` 并按最终分降序返回（``rrf_score`` 保持不变）。

    命中特征以 ``"<特征名> <分值>"`` 追加进 ``reasons``（可解释性，进 ContextPack reason）。
    ``candidates`` 为 ``Candidate``（非 frozen）→ 直接更新并返回同一批对象。
    """
    active_signals = signals or RerankSignals()
    active_weights = weights or weights_from_env()
    for candidate in candidates:
        base = candidate.rrf_score * RRF_BASE_SCALE
        hits = features(candidate, active_signals, active_weights)
        candidate.score = base + sum(value for _name, value in hits)
        for name, value in hits:
            reason = f"{name} {value:+.1f}"
            if reason not in candidate.reasons:
                candidate.reasons.append(reason)
    return sorted(candidates, key=lambda c: (-c.score, -c.rrf_score, c.chunk_id))


# --------------------------------------------------------------------------- 信号收集


def weights_from_env(source: Mapping[str, str] | None = None) -> RerankWeights:
    """按环境变量覆盖权重默认值（TASK-101 的实测调参入口，便于 A/B 而不改代码）。

    只覆盖 TASK-101 **新增**的 2 个权重（``NOVA_W_LITERAL`` / ``NOVA_W_LITERAL_ROOT``
    ）；R29/R30 冻结的旧权重不在此列——它们要等 TASK-093 的真实数据。
    缺省/非法值一律回落默认（与 ``IndexScope.from_env`` 同一纪律）。
    """
    env = os.environ if source is None else source
    base = RerankWeights()
    overrides: dict[str, float] = {}
    for name, variable in (
        ("literal_hit", LITERAL_WEIGHT_ENV),
        ("literal_root", LITERAL_ROOT_WEIGHT_ENV),
    ):
        raw = env.get(variable)
        if not raw:
            continue
        try:
            overrides[name] = float(raw)
        except ValueError:
            continue
    return replace(base, **overrides) if overrides else base


def collect_signals(
    store: Store,
    query: str,
    candidates: Sequence[Candidate],
    *,
    test_intent: bool | None = None,
    generated_paths: Iterable[str] | None = None,
    doctype_by_chunk: Mapping[str, str] | None = None,
    deprecated_chunk_ids: Iterable[str] | None = None,
) -> RerankSignals:
    """从索引 + 查询收集 rerank 需要的候选外信号。

    可显式覆盖 ``test_intent`` / ``generated_paths`` / ``doctype_by_chunk``（TASK-013 装配与
    测试用）；``generated_paths=None`` 时用 :func:`is_generated_path` 的文件名约定代理。
    """
    explicit = parse_explicit(query)
    query_symbols = frozenset(explicit.symbols) | frozenset(extract_inferred(query))

    explicit_chunk_ids = frozenset(c.chunk_id for c in candidates if _explicit_hit(c))
    ordered = sorted(candidates, key=lambda c: (-c.score, -c.rrf_score, c.chunk_id))
    top1 = ordered[0].chunk_id if ordered else None

    entry_points = _exported_chunk_ids(store, candidates)
    stale_chunks = _stale_spec_chunk_ids(store, candidates)

    if generated_paths is None:
        # P2-5：索引期写下的真实列（含内容 banner）与文件名约定取**并集**。
        # 为什么必须取并集而不是“列优先”：升级前建的索引里 ``generated`` 全是 0，
        # 若改为“读列就覆盖代理”，那些索引的 generated 识别会静默消失（降权失效 → 排名漂移），
        # 而调用方无从察觉。并集下：旧索引行为不变，新索引额外捕获内容 banner 标记的文件。
        generated = frozenset(c.path for c in candidates if c.path and is_generated_path(c.path))
        stored = _stored_generated_paths(store)
        if stored:
            generated |= frozenset(c.path for c in candidates if c.path and c.path in stored)
    else:
        generated = frozenset(generated_paths)

    if test_intent is None:
        test_intent = is_test_intent(query) or _query_names_test_symbol(store, query_symbols)

    if deprecated_chunk_ids is None:
        deprecated = _deprecated_chunk_ids(store, candidates)
    else:
        deprecated = frozenset(deprecated_chunk_ids)

    doctypes = (
        dict(doctype_by_chunk)
        if doctype_by_chunk is not None
        else {
            c.chunk_id: classify_doctype(c.path)
            for c in candidates
            if c.kind == KIND_SPEC and c.path
        }
    )

    return RerankSignals(
        query_symbols=query_symbols,
        explicit_chunk_ids=explicit_chunk_ids,
        top1_seed_chunk_id=top1,
        exported_chunk_ids=entry_points,
        generated_paths=generated,
        deprecated_chunk_ids=deprecated,
        stale_chunk_ids=stale_chunks,
        test_intent=test_intent,
        doctype_by_chunk=doctypes,
    )


def _deprecated_chunk_ids(store: Store, candidates: Sequence[Candidate]) -> frozenset[str]:
    """带 deprecation 装饰器的**符号**下所有候选 chunk（−0.35；TASK-MCP-BUDGET）。

    判据是**符号级**事实：同一符号只要有一个 chunk 被 ``@deprecated(...)`` 标注，
    该符号的所有 chunk 都降权。

    为什么必须升到符号级（实测踩到）：Python 的 ``@overload`` 会把一个函数拆成多个 chunk
    （``init_chat_model`` 在 classic 有 L36/47/58/72 四个），而 **``@deprecated`` 装饰器
    只出现在最后一个 overload 的 chunk 首行**。若只降权那一个 chunk，排第 1 的 L36（普通
    overload）原封不动，整个修复失效。

    不用“文件导入了 deprecation 工具”：实测会误伤跃包本身（``langchain_core`` 就是提供者，
    它的导入让真实实现 ``chat_models.py`` 被降权，LC-13 从 rank 2 掉到 3）。
    """
    marked = store.chunks_with_marker(
        [c.chunk_id for c in candidates if c.chunk_id], marker=DEPRECATED_MARKER
    )
    if not marked:
        return frozenset()
    # 标记 → 同一**文件内同符号**的全部候选 chunk（含未被直接标注的 overload 片段）。
    #
    # 为什么必须限定文件：``symbol_fqn`` 对模块级函数是**裸名**（``init_chat_model``），
    # 跃包与弃用层各有一份同名函数 → 只按 fqn 扩展会把跃包那份一起降权，
    # 两边同降 = 排名不变（实测踩到过）。同文件同符号才是“同一个函数的多个切片”。
    deprecated_keys = {
        (c.path, c.symbol_fqn)
        for c in candidates
        if c.chunk_id in marked and c.symbol_fqn and c.path
    }
    if not deprecated_keys:
        return marked
    return frozenset(
        c.chunk_id
        for c in candidates
        if c.chunk_id in marked or (c.path, c.symbol_fqn) in deprecated_keys
    )


def _exported_chunk_ids(store: Store, candidates: Sequence[Candidate]) -> frozenset[str]:
    """被导出符号的 chunk（+0.2，信号源 = ``symbols.is_exported``，Module/02 §4.5）。

    "入口点 = 无内部调用者且 is_exported" 是 is_exported 的子集，二者并集仍是 is_exported；
    入口点判定的另一用处在 ``expand`` 的 caller 爆炸截断排序（那里才需要 edges 信号）。
    """
    found: set[str] = set()
    for candidate in candidates:
        fqn = candidate.symbol_fqn
        if not fqn:
            continue
        if any(row.fqn == fqn and row.is_exported for row in store.exact_symbols(fqn, limit=None)):
            found.add(candidate.chunk_id)
    return frozenset(found)


def _stale_spec_chunk_ids(store: Store, candidates: Sequence[Candidate]) -> frozenset[str]:
    """带 stale 引用的 SpecBlock chunk（−0.8，G4 的 rerank 侧消费）。"""
    found: set[str] = set()
    for candidate in candidates:
        if candidate.kind != KIND_SPEC:
            continue
        if any(ref.stale for ref in store.spec_refs_for_spec(candidate.chunk_id)):
            found.add(candidate.chunk_id)
    return frozenset(found)


def _query_names_test_symbol(store: Store, query_symbols: frozenset[str]) -> bool:
    """“路由为 General 且查询符号命中 tests/ 路径” 的确定性判定（V1 第二条件）。"""
    for symbol in query_symbols:
        for row in store.exact_symbols(symbol, limit=None):
            if row.file_path and is_test_path(row.file_path):
                return True
    return False


def with_weights(weights: RerankWeights, **overrides: float) -> RerankWeights:
    """按字段名覆盖权重（TASK-015 校准入口；未知字段直接报错）。"""
    return replace(weights, **overrides)
