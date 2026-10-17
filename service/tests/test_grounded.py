import json

import pytest
from nova_core.types import ContextPack, EvidenceItem, Flow, FlowNode, Freshness, MissingEvidence
from nova_service.answer import AnswerResponseError, answer_question, build_user_prompt
from nova_service.config import Settings
from nova_service.grounded import render_grounded


def pack():
    return ContextPack(
        query="q", mode="deep", answerable=True, confidence="high", freshness=Freshness(),
        evidence=[EvidenceItem(
            id="E1", type="code", path="src/run.py", content=(
                "1 | def run():\n2 |     guard()\n3 |     return '[E99]'\n"
                "4 |     finish()\n……省略 96 行……"
            ), score=1, evidence_tier=0, reason="rerank diagnostic 123", lines=(1, 100),
            elided_lines=96,
        ), EvidenceItem(
            id="E2", type="code", path="unused.py", content="1 | unused_source()",
            score=1, evidence_tier=2, reason="noise", lines=(1, 1),
        )],
        flows=[Flow("F1", (FlowNode("run", "src/run.py", 1),), truncated=True)],
        missing_evidence=[MissingEvidence("dynamic_dispatch", "动态目标尚未解析。")],
    )


def test_compact_input_keeps_sources_not_ranking_diagnostics():
    data = pack()
    data.evidence[0].reason += " + synthesized edge"
    prompt = build_user_prompt(data, "How does run work?")
    assert "unverified binding" in prompt
    assert "rerank diagnostic" not in prompt
    assert "1 | def run():" in prompt and "[E2]" in prompt
    assert "src/run.py:1-100" in prompt
    assert "96 lines omitted" in prompt and "动态目标尚未解析" in prompt
    assert "not runtime execution" in prompt


def test_json_claims_and_only_selected_real_source_are_rendered():
    raw = json.dumps({
        "claims": [{"text": "先检查条件", "references": ["E1"]}],
        "evidence_ids": ["E1"],
        "source_ranges": [{"id": "E1", "start": 2, "end": 3}],
    })
    result = render_grounded(raw, pack())
    assert result is not None
    assert "先检查条件 [E1]" in result.answer
    assert "2 |     guard()" in result.answer
    assert "3 |     return '[E99]'" in result.answer  # 原源码不是模型引用
    assert "unused_source" not in result.answer
    assert "1 | def run" not in result.answer
    assert "动态目标尚未解析" in result.answer
    assert result.references == ("E1",)


@pytest.mark.parametrize("references", [[], ["E99"], ["E1", "F99"]])
def test_invalid_or_absent_reference_drops_whole_claim(references):
    result = render_grounded(json.dumps({
        "claims": [{"text": "错误事实不可保留", "references": references}],
        "evidence_ids": ["E1"],
    }), pack())
    assert result is not None
    assert "错误事实不可保留" not in result.answer
    assert "证据不足" in result.answer
    assert "## Key Evidence" not in result.answer


def test_elided_lines_and_boolean_ranges_are_not_emitted():
    result = render_grounded(json.dumps({
        "claims": [{"text": "检查条件", "references": ["E1"]}],
        "source_ranges": [
            {"id": "E1", "start": 80, "end": 81},
            {"id": "E1", "start": True, "end": 3},
        ],
    }), pack())
    assert result is not None
    assert "80 |" not in result.answer
    # 不可校验的源码范围静默丢弃：那是校验细节，不是证据缺口（不写进回答）。
    assert "2 个" not in result.answer


class Provider:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.raw


@pytest.mark.parametrize("raw", ['{"claims":', '{"claims":{},"gaps":[]}',
                                 '[{"claims":[]}]', "[",
                                 '```json\nnot JSON\n```', '"uncited summary"', "null",
                                 "[tru", "[fals", "[nul"])
def test_bad_json_is_one_call_then_existing_failure_type(raw):
    provider = Provider(raw)
    with pytest.raises(AnswerResponseError):
        answer_question(provider=provider, settings=Settings(), pack=pack(), question="q")
    assert len(provider.calls) == 1


@pytest.mark.parametrize("raw", [
    '{"claims":[{"text":"检查条件","references":["E1"]}]}',
    '```\n{"claims":[{"text":"检查条件","references":["E1"]}]}\n```',
    '```json\n{"claims":[{"text":"检查条件","references":["E1"]}]}\n```',
    '好的，结果如下：\n{"claims":[{"text":"检查条件","references":["E1"]}]}\n以上。',
    '{"claims":[{"text":"检查条件","references":["E1"]}],"gaps":[],"gaps":["重复键以后者为准"]}',
])
def test_structured_answer_tolerates_fence_prose_and_duplicate_keys(raw):
    result = render_grounded(raw, pack())
    assert result is not None and "检查条件" in result.answer and "[E1]" in result.answer


def test_markdown_with_braces_is_not_mistaken_for_structured():
    assert render_grounded("函数 `f` 的体是 {return 1}，见 [E1]。", pack()) is None


def test_structured_call_keeps_code_literal_and_legacy_markdown_still_works():
    provider = Provider(json.dumps({
        "claims": [{"text": "条件检查", "references": ["E1"]}],
        "source_ranges": [{"id": "E1", "start": 3, "end": 3}],
    }))
    outcome = answer_question(provider=provider, settings=Settings(), pack=pack(), question="q")
    assert "return '[E99]'" in outcome.answer
    assert outcome.invalid_citations == 0
    assert outcome.valid_citations == 1
    assert len(provider.calls) == 1
    legacy = Provider("## Answer\n保留 Markdown [E1]")
    answer = answer_question(provider=legacy, settings=Settings(), pack=pack(), question="q")
    assert answer.answer == "## Answer\n保留 Markdown [E1]"
    assert len(legacy.calls) == 1


def _pack_with_ids(count: int = 5):
    """一个带 ``count`` 条真实证据的包（E1..En），用于容忍度测试。"""
    from nova_core.types import Budget, ContextPack, EvidenceItem, Freshness

    items = [
        EvidenceItem(
            id=f"E{index}", type="code", path=f"src/m{index}.py", content=f"1 | x{index} = 1",
            score=1.0, evidence_tier=1, reason="exact", lines=(1, 1),
        )
        for index in range(1, count + 1)
    ]
    return ContextPack(
        "q", "deep", True, "medium", Freshness(), evidence=items, budget=Budget(0, 1, False, 0)
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda o: o.update(evidence_ids=["E1", "E2", "E3", "E4"]),
        lambda o: o.update(claims=[o["claims"][0]] * 9),
        lambda o: o["claims"][0].update(kind="opinion"),
        lambda o: o.update(gaps=["g"] * 7),
        lambda o: o["claims"][0].update(text="x" * 900),
        lambda o: o.update(flow_ids=["F1", "F2", "F3"]),
        lambda o: o.update(source_ranges="nope"),
        lambda o: o.update(gaps=[1, None, "ok"]),
    ],
    ids=["evidence>3", "claims>8", "kind", "gaps>6", "text>800", "flows>2",
         "ranges-type", "gaps-type"],
)
def test_shape_deviations_are_tolerated_not_fatal(mutate) -> None:
    """模型输出的**形状偏差**（超上限、类型/类别不对）只截断/降级，不丢弃整份有引用的答案。"""
    obj = {
        "claims": [{"text": "结论", "references": ["E1"], "kind": "fact"}],
        "evidence_ids": ["E1"], "flow_ids": [], "source_ranges": [], "gaps": [],
    }
    mutate(obj)
    result = render_grounded(json.dumps(obj, ensure_ascii=False), _pack_with_ids())
    assert result is not None and "[E1]" in result.answer


def test_invalid_references_are_still_rejected_per_claim() -> None:
    """真正的安全线不放松：引用了不存在的证据 id 的结论被移除，其余结论保留。"""
    obj = {
        "claims": [
            {"text": "有据", "references": ["E1"]},
            {"text": "幻觉", "references": ["E99"]},
        ],
    }
    result = render_grounded(json.dumps(obj), _pack_with_ids())
    assert "有据" in result.answer and "幻觉" not in result.answer
    assert result.invalid_references == 1


def test_render_repairs_citations_swallowed_into_claim_text() -> None:
    pack = ContextPack(
        query="q", mode="deep", answerable=True, confidence="high", freshness=Freshness(),
        evidence=[
            EvidenceItem(
                id=f"E{i}", type="code", path="a.py", content="1 | x", score=1.0,
                evidence_tier=1, reason="t", symbol=None, lines=(1, 1),
            )
            for i in (1, 4)
        ],
    )
    raw = '{"claims":[{"text":"parsed in app(). [E1","E4"],"kind":"fact"}],"gaps":[]}'
    result = render_grounded(raw, pack)
    assert result is not None and "parsed in app()." in result.answer
    assert result.references == ("E1", "E4") or list(result.references) == ["E1", "E4"]
