"""TASK-012 Markdown 渲染（DoD：完整 pack 的渲染快照 + 行号/⚠/Meta 齐全）。"""

from __future__ import annotations

from pathlib import Path

from nova_core.contextpack import (
    BudgetConfig,
    IndexSignals,
    assemble,
    render_evidence_for_prompt,
    render_markdown,
)
from nova_core.types import Flow, FlowNode, Freshness, SpecBlockDef

NOW = 1_760_000_300


def _spec_block() -> SpecBlockDef:
    return SpecBlockDef(
        path="docs/design/auth.md",
        heading="Token Refresh",
        heading_path="认证 > Token Refresh",
        level=2,
        start_line=30,
        end_line=32,
        content="刷新流程说明\n\n使用 refresh_token",
        doctype="design",
    )


def _build_pack(store, seed_file, sym, cand):
    seed_file(
        store,
        path="src/auth/token_service.py",
        symbols=[sym("refresh", "TokenService.refresh", kind="method", start=45, end=46)],
        bodies={"TokenService.refresh": "def refresh(self):\n    return self.store.rotate()"},
    )
    seed_file(
        store,
        path="src/store.py",
        symbols=[sym("rotate", "TokenStore.rotate", kind="method", start=210, end=212)],
        bodies={"TokenStore.rotate": "def rotate(self):\n    return 'rotated'\n"},
    )
    seed_file(
        store,
        path="src/mw.py",
        symbols=[sym("verify", "AuthMiddleware.verify", kind="method", start=88, end=89)],
        bodies={"AuthMiddleware.verify": "def verify(self, request):\n    return refresh()"},
    )
    seed_file(store, path="docs/design/auth.md", language="markdown", spec_blocks=[_spec_block()])
    store.add_spec_refs(
        [
            (
                "docs/design/auth.md:认证 > Token Refresh:30",
                "src/auth/token_service.py:TokenService.refresh:45",
            )
        ]
    )
    store.apply_deletions(["src/legacy.py"])

    candidates = [
        cand(
            "src/auth/token_service.py",
            "TokenService.refresh",
            45,
            score=3.5,
            tier=0,
            channels={"exact": 1, "bm25": 1, "vector": 1},
            reasons=["explicit symbol TokenService.refresh", "3-channel consensus +0.5"],
            end=46,
        ),
        cand(
            "src/store.py",
            "TokenStore.rotate",
            210,
            score=2.0,
            channels={"bm25": 2, "vector": 2},
            reasons=["bm25 -1.2", "vector 0.81"],
            end=212,
        ),
        cand(
            "src/mw.py",
            "AuthMiddleware.verify",
            88,
            score=1.8,
            channels={"bm25": 3, "vector": 3},
            reasons=["bm25 -1.0", "vector 0.77"],
            end=89,
        ),
        cand(
            "docs/design/auth.md",
            "认证 > Token Refresh",
            30,
            score=1.2,
            kind="spec",
            channels={"bm25": 1},
            reasons=["bm25 -2.1", "high-value doctype +0.8"],
        ),
    ]
    flow = Flow(
        id="F1",
        nodes=(
            FlowNode(symbol="AuthMiddleware.verify", path="src/mw.py", line=88),
            FlowNode(symbol="TokenService.refresh", path="src/auth/token_service.py", line=45),
        ),
        truncated=False,
    )
    freshness = Freshness(indexed_at=NOW - 300, stale_files=("src/old.py",))
    config = BudgetConfig(hard_cap=10_000, framework_overhead=500)
    signals = IndexSignals(
        stale_doc_refs={"docs/design/auth.md:认证 > Token Refresh:30": ("LegacyToken.rotate",)},
        unresolved_count=2,
    )
    return assemble(
        store,
        "token 过期后在哪里刷新？",
        candidates,
        flows=[flow],
        freshness=freshness,
        config=config,
        signals=signals,
    )


def test_render_section_order_and_details(store, seed_file, sym, cand) -> None:
    """既有四节的标题与顺序未变（TASK-087 回归保护：新节插在 Meta 之前）。

    TASK-096 §B-2 起：``_build_pack`` 是 ``answerable=true`` 的包 → ``next_queries`` 为空、
    该节不渲染。本节与顺序仍用**显式注入**的列表验证——渲染层与生成策略解耦。
    """
    pack = _build_pack(store, seed_file, sym, cand)
    assert pack.next_queries == [], "§B-2：answerable=true 不生成自愈查询"
    pack.next_queries = ["refresh 的调用方有哪些", "认证 > Token Refresh 对应的实现代码在哪里"]
    text = render_markdown(pack, now=NOW)

    order = [
        "## Relevant Context",
        "### Code",
        "### Flow",
        "### Docs",
        "### Missing Evidence",
        "### Suggested Next Queries",
        "### Meta",
    ]
    positions = [text.index(section) for section in order]
    assert positions == sorted(positions)

    assert "[E1] TokenService.refresh — src/auth/token_service.py:45-46" in text
    assert "45 | def refresh(self):" in text          # 行号（agent 可对齐 Edit）
    assert "46 |     return self.store.rotate()" in text
    assert "[F1] AuthMiddleware.verify → TokenService.refresh" in text
    assert "[E4] docs/design/auth.md > 认证 > Token Refresh（design）" in text
    assert "⚠ 引用了已删除符号 LegacyToken.rotate，文档可能过时" in text
    assert "- [index_stale] " in text
    assert "- [stale_doc_reference] (LegacyToken.rotate) " in text
    assert "confidence: high | index: stale (1 files) | budget: " in text


def test_render_meta_budget_and_index_fresh(store, seed_file, sym, cand) -> None:
    seed_file(store, path="src/a.py", symbols=[sym("f", "f", start=1)])
    pack = assemble(
        store,
        "q",
        [cand("src/a.py", "f", 1, score=1.0)],
        freshness=Freshness(indexed_at=NOW - 300),
    )
    text = render_markdown(pack, now=NOW)
    assert "index: fresh (5 min ago)" in text
    # TASK-096 §A：预算账 = 框架开销 + 渲染开销（header+reason+行号），不再是只算正文 → 504 → 515。
    # TASK-MCP-BUDGET：默认预算 10K→14K，分母随之变化（分子 515 与预算无关，未变）。
    assert "budget: 515/14.0K" in text   # <1000 用原值，≥1000 用 K（Module/03 §6 例）
    assert "confidence: low" in text


def test_render_evidence_for_prompt_has_no_meta_or_flow(store, seed_file, sym, cand) -> None:
    pack = _build_pack(store, seed_file, sym, cand)
    prompt_section = render_evidence_for_prompt(pack)
    assert "### Code" in prompt_section
    assert "### Docs" in prompt_section
    assert "### Meta" not in prompt_section
    assert "### Flow" not in prompt_section
    assert "### Missing Evidence" not in prompt_section
    assert "### Suggested Next Queries" not in prompt_section, (
        "本节是给 Agent 的；LLM prompt 的 evidence 分节不含它（TASK-087 §A）"
    )
    assert "[E1]" in prompt_section and "[E4]" in prompt_section


# ------------------------------------------------------------------ next_queries（§A）


def test_next_queries_section_lists_each_query_without_numbering(
    store, seed_file, sym, cand
) -> None:
    """``next_queries`` 非空 → 渲染 ``### Suggested Next Queries``，每行 ``- <query>``。

    渲染层只认 ``pack.next_queries`` 的内容（生成策略由 assembly 的 §B 管），
    故此处按既有惯例显式注入列表。
    """
    pack = _build_pack(store, seed_file, sym, cand)
    pack.next_queries = ["refresh 的调用方有哪些", "src/auth/token_service.py 里还有哪些符号"]
    text = render_markdown(pack, now=NOW)

    assert "### Suggested Next Queries" in text
    section = text.split("### Suggested Next Queries\n", 1)[1].split("\n### ", 1)[0]
    lines = section.splitlines()
    assert lines == [f"- {query}" for query in pack.next_queries]
    # 不加编号：正文里不应出现 ``- [N]`` 形态（会和 [E*] / [F*] 证据编号混淆）。
    assert "- [" not in section


def test_answerable_pack_renders_no_suggested_queries_section(
    store, seed_file, sym, cand
) -> None:
    """§B-2 端到端：``answerable=true`` → 无自愈查询 → 整节不渲染（不再建议"换个方式再问"）。"""
    pack = _build_pack(store, seed_file, sym, cand)
    assert pack.answerable is True
    text = render_markdown(pack, now=NOW)
    assert "Suggested Next Queries" not in text


def test_next_queries_empty_omits_the_section(store, seed_file, sym, cand) -> None:
    """``next_queries`` 为空 → 整节不渲染（空节白占 token）。"""
    seed_file(store, path="src/a.py", symbols=[sym("f", "f", start=1)])
    pack = assemble(
        store,
        "q",
        [cand("src/a.py", "f", 1, score=1.0)],
        freshness=Freshness(indexed_at=NOW - 300),
    )
    pack.next_queries = []
    md = render_markdown(pack, now=NOW)
    assert "Suggested Next Queries" not in md


def test_existing_sections_are_byte_identical_to_the_snapshot(store, seed_file, sym, cand) -> None:
    """快照式回归：注入自愈查询后，其余节逐字不变。

    两卡合并（2026-09-14）后的口径：

    - **TASK-095**：在 ``### Code`` **内部**新增了四级分组标题（``#### Core`` 等），
      但**没有**改顶层节名、节序或 ``[E*]`` 编号——本断言锁的正是这三样；
    - **TASK-096 §B-2**：``_build_pack`` 是 answerable 包 → ``next_queries`` 为空，
      基线（快照）里**没有** ``### Suggested Next Queries`` 节；
      注入 ``next_queries`` 后应**只在 Meta 之前多一块**。
    """
    pack = _build_pack(store, seed_file, sym, cand)
    expected = SNAPSHOT_PATH.read_text(encoding="utf-8").rstrip("\n")
    baseline = render_markdown(pack, now=NOW)
    assert baseline == expected, "answerable 包的渲染与快照逐字一致（无 Suggested Next Queries 节）"

    pack.next_queries = ["refresh 的调用方有哪些"]
    text = render_markdown(pack, now=NOW)
    assert text == baseline.replace(
        "### Meta", "### Suggested Next Queries\n- refresh 的调用方有哪些\n### Meta"
    ), "其余节逐字未变，只有新节插在 Meta 之前"

    # TASK-095：顶层节名与顺序逐字未变（四级标题不构成新顶层节）。
    # 注意：`text` 是注入了 next_queries 的（TASK-096 §B-2 口径），比 baseline 多一个
    # `### Suggested Next Queries` 节——所以先把它补回 baseline 再逐节比对。
    expected_sections = [
        line
        for line in baseline.replace(
            "### Meta", "### Suggested Next Queries\n### Meta"
        ).splitlines()
        if line.startswith("### ")
    ]
    actual_sections = [line for line in text.splitlines() if line.startswith("### ")]
    assert actual_sections == expected_sections, "顶层节名与顺序逐字未变（TASK-095）"
    assert "\n#### Core\n" in text, "§B 的分组标题必须真的出现"


def test_render_empty_pack_keeps_missing_and_meta(store) -> None:
    pack = assemble(store, "nothing", [])
    text = render_markdown(pack)
    assert "## Relevant Context" in text
    assert "### Missing Evidence" in text
    assert "- [no_context_match] " in text
    assert "### Meta" in text
    assert "### Code" not in text


SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "rich_pack.md"


def test_render_markdown_snapshot(store, seed_file, sym, cand) -> None:
    """完整 pack 的 Markdown 与固定快照一致（行号、⚠、Meta 齐全）。

    TASK-096 起快照反映：① 预算账 = 渲染账（Meta 里的 budget 数值变大）；
    ② ``answerable=true`` 无 Suggested Next Queries 节。
    """
    pack = _build_pack(store, seed_file, sym, cand)
    expected = SNAPSHOT_PATH.read_text(encoding="utf-8").rstrip("\n")
    assert render_markdown(pack, now=NOW) == expected
