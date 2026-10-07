"""TASK-022（R22）：answerable / confidence 判定收紧的回归测试。

背景（TASK-014 基线实测）：负例通过率 2/6，失败的 4 条全部是 `answerable=True`。
机制是判定口径 `answerable = explicit>=1 or consensus>=2 or structural`——在文档密集仓库里，
任何中文查询都能凑出 ≥2 个"BM25 + Vector 双命中"的文档候选（文档措辞与查询措辞同源，
且同一份长文档切出的小节会同时双命中），于是"双通道共识"退化成"有没有文档"。

收紧后的口径（数据见 TASK-022 执行记录）：

```text
answerable = explicit 命中 >= 1
           | inferred 命中 >= 1                      # 符号级命中，与本卡新增口径一致
           | structural_result
           | (共识候选跨 >=2 个文件 且 ①池内最高分候选被 >=2 通道命中
                                  或 ②共识最高分 >= 2.15 × 池分数中位数)
answerable=False → confidence 一律 low（不用中等把握掩盖不可回答）
```

本文件覆盖任务卡 DoD 的三项：
1. 负例场景（高频词命中多文档但无实质内容）→ `answerable=False`；
2. 正例场景（Explicit 命中 / 双通道真共识）→ `answerable=True`；
3. `answerable=False` 时 `confidence=low`。
"""

from __future__ import annotations

from nova_core.contextpack import BudgetConfig, assemble
from nova_core.types import SpecBlockDef

NOISE_PATH = "docs/记忆系统调研.md"
OTHER_PATH = "docs/架构总览.md"
CONFIG = BudgetConfig(hard_cap=100_000, framework_overhead=0, single_file_ratio=1.0)


def _spec(path: str, heading: str, *, start: int) -> SpecBlockDef:
    return SpecBlockDef(
        path=path,
        heading=heading.rsplit(" > ", 1)[-1],
        heading_path=heading,
        level=2,
        start_line=start,
        end_line=start + 4,
        content=f"{heading}：记忆检索 执行流程 实现 逻辑 协调 部署 " * 8,
        doctype="guide",
    )


def test_doc_dense_noise_pool_is_not_answerable(store, seed_file, cand) -> None:
    """负例形态：文档密集、措辞同源导致双通道命中，但分数没有突出者、且没有跨文件共识。

    对照真实用例 `aibox-0008`（"Kubernetes operator 的部署协调逻辑在哪里实现？"）：
    池内最高分是一条**单通道**文档（README 样板文），双通道共识只是同一批文档的措辞巧合。
    """
    seed_file(
        store,
        path=NOISE_PATH,
        language="markdown",
        spec_blocks=[_spec(NOISE_PATH, "记忆系统调研 > 检索流程", start=10)],
    )
    seed_file(
        store,
        path=OTHER_PATH,
        language="markdown",
        spec_blocks=[_spec(OTHER_PATH, "架构总览 > 检索流程", start=20)],
    )
    candidates = [
        # 最高分候选只有单通道（噪声文档，措辞与查询同源）
        cand(NOISE_PATH, "记忆系统调研 > 检索流程", 10, score=2.20, kind="spec",
             channels={"bm25": 1}),
        # 双通道共识存在，但彼此分数接近（远未达到 2.15× 中位数）
        cand(NOISE_PATH, "记忆系统调研 > 检索流程", 10, score=2.16, kind="spec",
             channels={"bm25": 2, "vector": 2}, end=14),
        cand(OTHER_PATH, "架构总览 > 检索流程", 20, score=2.14, kind="spec",
             channels={"bm25": 3, "vector": 3}, end=24),
    ]

    pack = assemble(store, "记忆检索的执行流程在哪个文件里实现？", candidates, config=CONFIG)

    assert pack.answerable is False
    assert pack.confidence == "low"
    # 注：实盘上 negative 的 `missingEvidence` 恒非空（unresolved_reference 等信号）；
    # 本合成夹具没有 freshness/stale/unresolved 信号，故不在此断言（runner 负例判定在实盘生效）。


def test_single_file_multi_section_consensus_is_not_answerable(store, seed_file, cand) -> None:
    """同一份长文档的多个小节同时双命中 ≠ 多来源共识（堵住"文档切片放大"造成的伪共识）。"""
    seed_file(
        store,
        path=NOISE_PATH,
        language="markdown",
        spec_blocks=[
            _spec(NOISE_PATH, "记忆系统调研 > 检索流程", start=10),
            _spec(NOISE_PATH, "记忆系统调研 > 写入流程", start=40),
        ],
    )
    candidates = [
        cand(NOISE_PATH, "记忆系统调研 > 检索流程", 10, score=3.0, kind="spec",
             channels={"bm25": 1, "vector": 1}),
        cand(NOISE_PATH, "记忆系统调研 > 写入流程", 40, score=2.9, kind="spec",
             channels={"bm25": 2, "vector": 2}),
    ]

    pack = assemble(store, "记忆检索的执行流程在哪个文件里实现？", candidates, config=CONFIG)

    assert pack.answerable is False
    assert pack.confidence == "low"


def test_explicit_symbol_hit_is_answerable(store, seed_file, sym, cand) -> None:
    """正例形态 1：Explicit 符号命中是硬依据（不依赖共识强度）。"""
    seed_file(store, path="src/token_service.py", symbols=[sym("refresh", "TokenService.refresh")])
    candidates = [
        cand("src/token_service.py", "TokenService.refresh", 1, score=2.0,
             channels={"exact": 1, "bm25": 1, "vector": 1}),
        cand("docs/token.md", "令牌设计", 1, score=1.9, kind="spec",
             channels={"bm25": 1, "vector": 1}),
    ]

    pack = assemble(store, "TokenService.refresh", candidates, config=CONFIG)

    assert pack.answerable is True
    assert pack.confidence in {"low", "medium", "high"}


def test_inferred_symbol_hit_is_answerable(store, seed_file, sym, cand) -> None:
    """正例形态 1b：Inferred 符号命中（02 Exact-Inferred 通道）同样算符号级依据。"""
    seed_file(store, path="src/hash.py", symbols=[sym("HashEmbedding", "HashEmbedding")])
    candidates = [
        cand("src/hash.py", "HashEmbedding", 1, score=2.0, channels={"inferred": 1, "vector": 1}),
        cand("docs/x.md", "设计", 1, score=1.9, kind="spec", channels={"bm25": 1}),
    ]

    pack = assemble(store, "deterministic hash embedding 类在哪", candidates, config=CONFIG)

    assert pack.answerable is True


def test_cross_file_strong_consensus_is_answerable(store, seed_file, sym, cand) -> None:
    """正例形态 2：跨文件双通道共识 + 最高分显著高于池中位数 → 有据可依（confidence=medium）。"""
    seed_file(store, path="src/a.py", symbols=[sym("a", "a")])
    seed_file(store, path="src/b.py", symbols=[sym("b", "b")])
    seed_file(
        store,
        path="docs/design.md",
        language="markdown",
        spec_blocks=[_spec("docs/design.md", "架构 > 缓存", start=10)],
    )
    candidates = [
        cand("src/a.py", "a", 1, score=4.0, channels={"bm25": 1, "vector": 1}),
        cand("src/b.py", "b", 1, score=3.8, channels={"bm25": 2, "vector": 2}),
        cand("docs/design.md", "架构 > 缓存", 10, score=1.0, kind="spec", channels={"bm25": 3}),
    ]

    pack = assemble(store, "缓存失效后在哪里重算？", candidates, config=CONFIG)

    assert pack.answerable is True
    assert pack.confidence == "medium"


def test_corroborated_top_candidate_is_answerable_even_in_tied_pool(store, seed_file, sym, cand):
    """正例形态 2b：池内最高分候选被双通道印证且共识跨 ≥2 文件 → 即使分数接近也算有据。

    这条覆盖 M1 集成口径（`core/tests/integration/test_m1_pipeline.py` 断言 2）：
    小语料里分值天然接近，"中位数倍数"不适用，但"最强证据被两条通道同时命中 + 跨文件"仍成立。
    """
    seed_file(store, path="src/a.py", symbols=[sym("a", "a")])
    seed_file(
        store,
        path="docs/design.md",
        language="markdown",
        spec_blocks=[_spec("docs/design.md", "架构 > 令牌", start=10)],
    )
    candidates = [
        cand("src/a.py", "a", 1, score=4.08, channels={"bm25": 1, "vector": 1}),
        cand("docs/design.md", "架构 > 令牌", 10, score=4.00, kind="spec",
             channels={"bm25": 2, "vector": 2}),
        cand("docs/design.md", "架构 > 令牌", 10, score=3.30, kind="spec", channels={"bm25": 3}),
    ]

    pack = assemble(store, "令牌过期后在哪里刷新", candidates, config=CONFIG)

    assert pack.answerable is True
