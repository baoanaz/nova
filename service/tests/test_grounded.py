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
    prompt = build_user_prompt(pack(), "How does run work?")
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
    assert "2 个" in result.answer


class Provider:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def complete(self, **kwargs):
        self.calls.append(kwargs)
        return self.raw


@pytest.mark.parametrize("raw", ['{"claims":', '{"claims":{},"gaps":[]}',
                                 '{"claims":[],"claims":[]}', '[{"claims":[]}]', "[",
                                 '```json\nnot JSON\n```', '"uncited summary"', "null",
                                 "[tru", "[fals", "[nul"])
def test_bad_json_is_one_call_then_existing_failure_type(raw):
    provider = Provider(raw)
    with pytest.raises(AnswerResponseError):
        answer_question(provider=provider, settings=Settings(), pack=pack(), question="q")
    assert len(provider.calls) == 1


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
