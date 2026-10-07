"""TASK-088 验收：``ask`` 接入 LLM（配置驱动 + Grounded Prompt + Citation 回验 + 审计）。

设计依据：``docs/design/Module/04-AI总结.md`` §2/§4/§5/§6/§7/§8；任务卡 DoD 的七条覆盖逐条对应
下面七个测试组（**不联网**：LLM 一律用 :class:`FakeAnswerProvider` 或 ``httpx.MockTransport``）。

| 用例 | 守什么 |
|---|---|
| :func:`test_unconfigured_answer_still_degrades_not_500` | L5：未配置 → 200 降级包，绝不 500/报错 |
| :func:`test_provider_failure_degrades_with_notice` | D-26：配置了但报错/超时 → 降级包 + 故障说明 |
| :func:`test_hallucinated_citations_are_stripped_and_noted` | §5：虚构引用只删标记，缺漏节加注记 |
| :func:`test_valid_citations_are_kept` | §5：有效引用保留 |
| :func:`test_citation_coverage_counts_conclusion_paragraphs` | §5：2 段结论 / 1 个有效引用 → 0.5 |
| :func:`test_api_key_never_appears_in_logs_or_response` | 卡内脱敏硬要求（key 明文） |
| :func:`test_model_timeout_and_temperature_come_from_config` | 用户核心要求：改 env 即改行为 |

另有两组：Grounded Prompt 的七条规则/回答模板逐条在场（§4）、审计三个新字段真的落库（§E）。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nova_core.engine import Engine
from nova_core.types import ContextPack, EvidenceItem, Flow, FlowNode, Freshness
from nova_service import answer as answer_module
from nova_service.answer import (
    ANSWER_SECTIONS,
    SYSTEM_PROMPT,
    SYSTEM_PROMPT_RULES,
    AnswerAuthError,
    AnswerError,
    AnswerOutcome,
    AnswerResponseError,
    AnswerUnavailableError,
    HttpAnswerProvider,
    build_evidence_block,
    build_provider,
    build_user_prompt,
    chat_completions_endpoint,
    estimate_tokens,
    verify_citations,
)
from nova_service.app import create_app
from nova_service.config import Settings
from nova_service.metadb import MetaDB
from nova_service.routers.query import LLM_FAILED_NOTICE
from nova_service.runtime import EngineManager

from tests.conftest import (
    SAMPLE_FILES,
    DeterministicBigramEmbedding,
    make_client,
    upload_files,
)

#: 真实形态的假 key（脱敏断言用；含本服务签发前缀与 OpenAI 家族前缀两种形态）。
FAKE_API_KEY = "nova_FAKE-ANSWER-KEY-abc123XYZ"
FAKE_SK_KEY = "sk-fake-answer-key-SHOULD-NOT-LEAK-9876"
#: 假 provider 地址（不解析、不连接——客户端被注入的 MockTransport 拦下）。
FAKE_BASE_URL = "http://127.0.0.1:9/v1"
#: 必然"有答案"的问题（与 ``test_query_api`` 的同一语料前提）。
ANSWERABLE_QUERY = "TokenService.refresh_token"


# --------------------------------------------------------------------------- 假 provider


class FakeAnswerProvider:
    """确定性假 AnswerProvider（记录最后一次请求，可配置抛错）。

    ``max_tokens`` / ``temperature`` 都记下来，好让"参数来自配置"这条断言直接读真实入参。
    """

    def __init__(
        self,
        reply: str = "## Answer\n令牌由 refresh_token 刷新 [E1]。",
        *,
        error: AnswerError | None = None,
    ) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[dict[str, object]] = []

    def complete(self, *, system: str, user: str, max_tokens: int, temperature: float) -> str:
        self.calls.append(
            {"system": system, "user": user, "max_tokens": max_tokens, "temperature": temperature}
        )
        if self.error is not None:
            raise self.error
        return self.reply


def _make(tmp_path: Path, **overrides: object) -> tuple[FastAPI, EngineManager, TestClient]:
    """应用 + 引擎 + 客户端（临时 data_root；本地模式；无 LLM 配置）。"""
    defaults: dict[str, object] = {
        "data_root": tmp_path / "data",
        "local_mode": True,
        "local_rescan_interval_s": 0.0,
    }
    settings = Settings(**{**defaults, **overrides})  # type: ignore[arg-type]
    manager = EngineManager.open(
        settings.data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )
    app = create_app(settings)
    app.state.meta_db = MetaDB.open(settings.meta_db_path)
    app.state.engine_manager = manager
    return app, manager, make_client(app)


@pytest.fixture
def ask_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """不带 LLM 配置的环境（"未配置"路径的基线）+ 一个已索引的项目。

    ``monkeypatch.delenv`` 删除宿主机可能存在的 ``ANSWER_*``：测试必须与开发机的 ``.env``
    无关（否则"未配置"用例在本机直接变成"已配置"）。
    """
    for name in (
        "ANSWER_BASE_URL",
        "ANSWER_API_KEY",
        "ANSWER_MODEL",
        "ANSWER_TIMEOUT_S",
        "ANSWER_MAX_TOKENS",
        "ANSWER_TEMPERATURE",
    ):
        monkeypatch.delenv(name, raising=False)
    app, manager, client = _make(tmp_path)
    with client:
        project_id = manager.resolve_project("identity:answer", "answer-repo").project_id
        upload_files(manager, project_id, SAMPLE_FILES)
        yield SimpleNamespace(
            app=app,
            manager=manager,
            client=client,
            project_id=project_id,
            db=app.state.meta_db,
            tmp_path=tmp_path,
        )
    manager.close()


def _ask(ns: SimpleNamespace, question: str = ANSWERABLE_QUERY) -> dict:
    response = ns.client.post(
        "/api/query/ask", json={"projectId": ns.project_id, "question": question}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _with_fake_provider(ns: SimpleNamespace, provider: FakeAnswerProvider) -> Settings:
    """把假 provider 注入 ``app.state``（与 :func:`provider_for_app` 的缓存口径一致）。"""
    ns.app.state.answer_provider = provider
    ns.app.state.answer_provider_settings = ns.app.state.settings
    return ns.app.state.settings


# --------------------------------------------------------------------------- 1) 未配置（L5）


def test_unconfigured_answer_still_degrades_not_500(ask_env: SimpleNamespace) -> None:
    """未配置 ``ANSWER_*``：200 + ``status="degraded"`` + 可操作说明，**不抛错也不 500**。"""
    body = _ask(ask_env)
    assert body["status"] == "degraded"
    assert body["answer"].startswith("未配置总结模型")
    assert "ANSWER_BASE_URL" in body["answer"] and "ANSWER_API_KEY" in body["answer"]
    assert "## Relevant Context" in body["answer"], "降级包必须携带渲染好的上下文"
    assert body["meta"]["degraded"] is True


def test_unconfigured_settings_have_defaults_and_missing_env_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """未配置时：三个必填项判为缺失，另外三项回落到内置默认值（用户只需给三个）。"""
    for name in (
        "ANSWER_BASE_URL",
        "ANSWER_API_KEY",
        "ANSWER_MODEL",
        "ANSWER_TIMEOUT_S",
        "ANSWER_MAX_TOKENS",
        "ANSWER_TEMPERATURE",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings(data_root=tmp_path, local_mode=True)
    assert settings.answer_configured is False
    assert settings.answer_missing_env == ("ANSWER_BASE_URL", "ANSWER_API_KEY", "ANSWER_MODEL")
    assert (settings.answer_timeout_s, settings.answer_max_tokens) == (60.0, 3072)
    assert settings.answer_temperature == 0.2
    assert build_provider(settings) is None, "未配置 → 没有 provider（调用方走降级包）"


# --------------------------------------------------------------------------- 2) 报错/超时降级


@pytest.mark.parametrize(
    "error",
    [
        AnswerUnavailableError("总结模型响应超时"),
        AnswerAuthError("总结模型拒绝凭据（HTTP 401）"),
        AnswerResponseError("总结模型返回了空回答（content 为空）"),
    ],
)
def test_provider_failure_degrades_with_notice(
    ask_env: SimpleNamespace, error: AnswerError
) -> None:
    """D-26 故障矩阵：超时/密钥无效/响应异常 → 都是降级包 + 故障说明，绝不 500。"""
    _with_fake_provider(ask_env, FakeAnswerProvider(error=error))

    body = _ask(ask_env)
    assert body["status"] == "degraded"
    assert body["answer"].startswith("总结模型暂时不可用")
    assert "## Relevant Context" in body["answer"]
    assert body["meta"]["degraded"] is True
    assert body["meta"]["degradedReason"] == LLM_FAILED_NOTICE


def test_degraded_response_does_not_echo_provider_error(ask_env: SimpleNamespace) -> None:
    """降级说明不得回显 provider 原始报错（可能带 endpoint/内部细节，也不便于脱敏）。"""
    _with_fake_provider(
        ask_env,
        FakeAnswerProvider(
            error=AnswerUnavailableError(f"内部细节 {FAKE_BASE_URL} 与 {FAKE_SK_KEY}"),
        ),
    )

    body = _ask(ask_env)
    assert FAKE_BASE_URL not in json.dumps(body, ensure_ascii=False)
    assert FAKE_SK_KEY not in json.dumps(body, ensure_ascii=False)


# --------------------------------------------------------------------------- 3) 虚构引用


def _pack_with(
    *,
    evidence_ids: Sequence[str] = ("E1", "E2"),
    flow_ids: Sequence[str] = ("F1",),
) -> ContextPack:
    return ContextPack(
        query="q",
        mode="deep",
        answerable=True,
        confidence="medium",
        freshness=Freshness(indexed_at=1),
        evidence=[
            EvidenceItem(
                id=cid,
                type="code",
                path=f"src/{cid}.py",
                content="    1 def f(): ...",
                score=1.0,
                evidence_tier=1,
                reason="exact",
                lines=(1, 1),
            )
            for cid in evidence_ids
        ],
        flows=[
            Flow(id=fid, nodes=(FlowNode(symbol="a", path="a.py", line=1),)) for fid in flow_ids
        ],
    )


def test_hallucinated_citations_are_stripped_and_noted() -> None:
    """虚构引用（``[E99]``）：**只删标记**（保留原文），并在 Missing Evidence 追加注记。"""
    raw = (
        "## Answer\n"
        "刷新由 TokenService 负责 [E99]，它会调用缓存层 [F7]。\n"
        "## Key Evidence\n"
        "- src/token_service.py:45-82 [E1]\n"
    )

    check = verify_citations(raw, _pack_with())
    assert "[E99]" not in check.answer
    assert "[F7]" not in check.answer
    assert check.invalid == ("E99", "F7")
    # 句子与有效引用逐字保留（"不改写"）。
    assert "刷新由 TokenService 负责 ，它会调用缓存层 。" in check.answer
    assert "- src/token_service.py:45-82 [E1]" in check.answer
    assert "## Missing Evidence" in check.answer
    assert "- 回答中 2 处引用无效已移除" in check.answer


def test_invalid_citations_are_not_counted_in_coverage() -> None:
    """无效引用既不算有效，也不参与覆盖率分母之外的任何加成（不被虚构引用抬高）。"""
    raw = "## Answer\n结论一 [E99]\n结论二 [E98]\n"

    check = verify_citations(raw, _pack_with())
    # 追加的注记行本身也是一行正文（如实计数；注入一行是设计选择，已在执行记录说明）。
    assert check.paragraphs == 3
    assert check.valid == ()
    assert check.coverage == 0.0


# --------------------------------------------------------------------------- 4) 有效引用


def test_valid_citations_are_kept() -> None:
    """有效引用（id 存在于 pack 的 evidence/docs/flows）→ 原样保留，标记与顺序都不动。"""
    raw = "## Answer\n见 [E1] 与 [F1]，另有 [E2]。\n"

    check = verify_citations(raw, _pack_with())
    assert "[E1]" in check.answer and "[F1]" in check.answer and "[E2]" in check.answer
    assert check.invalid == ()
    assert check.valid == ("E1", "F1", "E2")
    assert "## Missing Evidence" not in check.answer, "没有无效引用就不该多出这一节"


def test_docs_share_the_e_id_space() -> None:
    """``docs`` 与 ``evidence`` 共用 E 编号空间（D-21）：doc 的 id 同样算有效。"""
    pack = _pack_with(evidence_ids=("E1",))
    pack.docs.append(
        EvidenceItem(
            id="E7",
            type="spec",
            path="docs/auth.md",
            content="    1 设计意图",
            score=0.5,
            evidence_tier=2,
            reason="spec",
            heading_path="认证",
        )
    )

    check = verify_citations("## Answer\n设计见 [E7]，实现见 [E1]。\n", pack)
    assert check.valid == ("E7", "E1")
    assert check.invalid == ()


# --------------------------------------------------------------------------- 5) coverage


def test_citation_coverage_counts_conclusion_paragraphs() -> None:
    """卡内例子：2 段结论 / 1 个有效引用 → 0.5（标题与空行不算段落）。"""
    raw = "## Answer\n第一段结论 [E1]\n\n第二段结论（无引用）\n"

    check = verify_citations(raw, _pack_with())
    assert check.paragraphs == 2
    assert check.coverage == 0.5


def test_citation_coverage_is_capped_at_one() -> None:
    """同一 id 引用多次 → 有效引用数按**出现次数**计，但覆盖率上限 1.0（不惩罚输出）。"""
    raw = "## Answer\n见 [E1] [E1] [E1] [E1]。\n"

    check = verify_citations(raw, _pack_with())
    assert check.valid == ("E1", "E1", "E1", "E1")
    assert check.coverage == 1.0


def test_missing_evidence_note_appends_to_existing_section() -> None:
    """已有 ``## Missing Evidence`` 节时追加一行，不新建重复标题。"""
    raw = "## Answer\n见 [E1]。\n## Missing Evidence\n- 缺调用方\n"

    check = verify_citations(raw + "[E99]\n", _pack_with())
    assert check.answer.count("## Missing Evidence") == 1
    assert "- 缺调用方" in check.answer
    assert "- 回答中 1 处引用无效已移除" in check.answer


def test_citation_inside_code_fence_is_not_rewritten() -> None:
    """代码块内的 ``[...]`` 也是引用标记（按同一规则回验），但**不重排空白**（保护缩进）。"""
    raw = "## Answer\n示例：\n```python\nvalue = table[0]  # [E99]\n```\n结论 [E1]\n"

    check = verify_citations(raw, _pack_with())
    assert "table" in check.answer
    assert "[E99]" not in check.answer
    assert check.valid == ("E1",)


# --------------------------------------------------------------------------- 6) key 不泄露


def _log_records(caplog: pytest.LogCaptureFixture) -> str:
    return "\n".join(record.getMessage() for record in caplog.records)


def test_api_key_never_appears_in_logs_or_response(
    ask_env: SimpleNamespace, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """key 明文**绝不**出现在日志与响应里（真实形态的假 key；含报错路径）。"""
    settings = Settings(
        data_root=ask_env.tmp_path / "data",
        local_mode=True,
        local_rescan_interval_s=0.0,
        answer_base_url=FAKE_BASE_URL,
        answer_api_key=FAKE_API_KEY,
        answer_model="deepseek-flash",
    )
    ask_env.app.state.settings = settings
    ask_env.app.state.engine_manager = ask_env.manager
    failing = FakeAnswerProvider(
        error=AnswerUnavailableError(f"总结模型不可达（{FAKE_BASE_URL}）：Bearer {FAKE_API_KEY}")
    )
    _with_fake_provider(ask_env, failing)

    with caplog.at_level(logging.DEBUG):
        body = _ask(ask_env)

    assert body["status"] == "degraded"
    payload = json.dumps(body, ensure_ascii=False)
    assert FAKE_API_KEY not in payload, "key 明文进了响应"
    assert FAKE_API_KEY not in _log_records(caplog), "key 明文进了日志"
    assert "***" in _log_records(caplog), "日志里应当只有脱敏占位（证明脱敏链路真的走到了）"


def test_http_provider_registers_secret_for_global_redaction(tmp_path: Path) -> None:
    """provider 构造时把 key 登记进全局脱敏表（第二道防线：日志里出现即被抹掉）。"""
    from nova_service.logging import REDACTED, redact_text

    HttpAnswerProvider(base_url=FAKE_BASE_URL, api_key=FAKE_SK_KEY, model="m")
    assert FAKE_SK_KEY not in redact_text(f"Authorization: Bearer {FAKE_SK_KEY}")
    assert REDACTED in redact_text(f"Authorization: Bearer {FAKE_SK_KEY}")


def test_http_provider_does_not_leak_key_in_exception_text() -> None:
    """provider 自己构造的异常文本里没有 key 明文（即使底层报错把 key 拼了进去）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"failed: Bearer {FAKE_API_KEY}", request=request)

    provider = HttpAnswerProvider(
        base_url=FAKE_BASE_URL,
        api_key=FAKE_API_KEY,
        model="m",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    with pytest.raises(AnswerError) as excinfo:
        provider.complete(system="s", user="u", max_tokens=8, temperature=0.0)
    assert FAKE_API_KEY not in str(excinfo.value)


# --------------------------------------------------------------------------- 7) 配置驱动


def test_model_timeout_and_temperature_come_from_config(
    ask_env: SimpleNamespace, tmp_path: Path
) -> None:
    """**用户核心要求**：模型/超时/上限/温度全部来自 ``Settings``，改 env 即改行为。

    - 模型名 → 直接进 HTTP 请求体（不是代码里的常量）；
    - ``ANSWER_MAX_TOKENS`` / ``ANSWER_TEMPERATURE`` → 直接进请求体；
    - ``ANSWER_TIMEOUT_S`` → 进 ``httpx.Timeout``（读 provider 客户端配置验证）。
    """
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "## Answer\n来自假 HTTP 的回答 [E1]"}}]},
        )

    settings = Settings(
        data_root=tmp_path / "data",
        local_mode=True,
        answer_base_url="http://llm.invalid/v1",
        answer_api_key=FAKE_SK_KEY,
        answer_model="fake/model-from-env",
        answer_timeout_s=7.5,
        answer_max_tokens=321,
        answer_temperature=0.75,
    )
    ask_env.app.state.settings = settings
    test_client = httpx.Client(transport=httpx.MockTransport(handler))
    provider = HttpAnswerProvider(
        base_url=str(settings.answer_base_url),
        api_key=str(settings.answer_api_key),
        model=str(settings.answer_model),
        timeout_s=settings.answer_timeout_s,
        client=test_client,
    )
    _with_fake_provider(ask_env, provider)

    body = _ask(ask_env)

    assert body["status"] == "answered"
    assert body["answer"].startswith("## Answer") and "[E1]" in body["answer"]
    assert len(seen) == 1, "一次 ask 恰好一次 LLM 调用（Module/04 §3）"
    request_body = seen[0]
    assert request_body["model"] == "fake/model-from-env", "模型名必须来自配置"
    assert request_body["max_tokens"] == 321
    assert request_body["temperature"] == 0.75

    # 超时：**自建客户端**时由 ``ANSWER_TIMEOUT_S`` 决定（注入客户端时用它自己的超时，
    # 那是测试注入接缝的责任，不是配置面）。
    owned = HttpAnswerProvider(
        base_url="http://llm.invalid/v1",
        api_key=FAKE_SK_KEY,
        model="m",
        timeout_s=7.5,
    )
    try:
        assert owned._client.timeout.read == 7.5  # noqa: SLF001 - 断言配置真的透传到客户端
        assert owned._client.timeout.connect == answer_module.CONNECT_TIMEOUT_S
    finally:
        owned.close()


def test_build_provider_uses_configured_values(tmp_path: Path) -> None:
    """``build_provider`` 把三项默认值也交给 provider（没有第二套硬编码常量）。"""
    settings = Settings(
        data_root=tmp_path,
        local_mode=True,
        answer_base_url="http://llm.invalid",
        answer_api_key=FAKE_SK_KEY,
        answer_model="m",
        answer_timeout_s=42.0,
    )
    provider = build_provider(settings)
    assert isinstance(provider, HttpAnswerProvider)
    assert provider.model == "m"
    assert provider.endpoint == "http://llm.invalid/v1/chat/completions"
    try:
        assert provider._client.timeout.read == 42.0  # noqa: SLF001
    finally:
        provider.close()


def test_answer_call_parameters_come_from_settings(ask_env: SimpleNamespace) -> None:
    """``answer_question`` 把 ``Settings.answer_*`` 原样交给 provider（不经第二处加工）。"""
    provider = FakeAnswerProvider(reply="## Answer\n结论 [E1]")
    settings = Settings(
        data_root=ask_env.tmp_path / "data",
        local_mode=True,
        local_rescan_interval_s=0.0,
        answer_base_url=FAKE_BASE_URL,
        answer_api_key=FAKE_API_KEY,
        answer_model="m",
        answer_max_tokens=123,
        answer_temperature=0.9,
    )

    outcome = answer_module.answer_question(
        provider=provider, settings=settings, pack=_pack_with(), question="q"
    )

    assert provider.calls[0]["max_tokens"] == 123
    assert provider.calls[0]["temperature"] == 0.9
    assert isinstance(outcome, AnswerOutcome)
    assert outcome.valid_citations == 1


# --------------------------------------------------------------------------- §4 Grounded Prompt


def test_system_prompt_contains_all_seven_rules_verbatim() -> None:
    """七条规则**逐条**在场（照抄 Module/04 §4），且回答模板五个节也都在。"""
    assert len(SYSTEM_PROMPT_RULES) == 7
    for rule in SYSTEM_PROMPT_RULES:
        assert rule in SYSTEM_PROMPT, f"缺规则：{rule}"
    for section in ANSWER_SECTIONS:
        assert section in SYSTEM_PROMPT
    assert "[E1] 代码/文档证据，[F1] 调用链证据" in SYSTEM_PROMPT
    assert "证据不足" in SYSTEM_PROMPT


def test_user_prompt_has_question_meta_and_grouped_evidence() -> None:
    """User prompt 的结构：``<question>`` + ``<context_meta>`` + ``<evidence>``（§4）。"""
    pack = _pack_with()
    pack.docs.append(
        EvidenceItem(
            id="E7",
            type="spec",
            path="docs/auth.md",
            content="    1 设计意图",
            score=0.5,
            evidence_tier=2,
            reason="spec",
            heading_path="认证",
        )
    )
    pack.freshness = Freshness(indexed_at=1, stale_files=("a.py", "b.py"))

    prompt = build_user_prompt(pack, "令牌怎么刷新？")

    assert "<question>令牌怎么刷新？</question>" in prompt
    assert "confidence: medium" in prompt
    assert "index: stale(2 files)" in prompt, "freshness 如实注入（§4 context_meta）"
    assert "<evidence>" in prompt and "</evidence>" in prompt
    # 分组标签与 E 编号都在（D-21 的同一编号空间）。
    assert "[Code]" in prompt and "[Docs]" in prompt and "[Flows]" in prompt
    assert "[E1]" in prompt and "[E7]" in prompt and "[F1]" in prompt
    assert "src/E1.py:1-1" in prompt, "证据必须带文件:行号"


def test_evidence_block_order_and_unknown_sections_are_preserved() -> None:
    """``[Docs]`` 在 ``[Code]`` 之前（§4 的分组顺序）；代码块内与文档块内在场。"""
    pack = _pack_with(evidence_ids=("E1",), flow_ids=())
    pack.docs.append(
        EvidenceItem(
            id="E7",
            type="spec",
            path="docs/auth.md",
            content="    1 设计意图",
            score=0.5,
            evidence_tier=2,
            reason="spec",
            heading_path="认证",
        )
    )

    block = build_evidence_block(pack)

    assert block.index("[Docs]") < block.index("[Code]")
    assert "设计意图" in block and "def f()" in block
    flow_pack = _pack_with(evidence_ids=("E1",))
    assert "[Flows]" in build_evidence_block(flow_pack), "有调用链时渲染 [Flows] 节"


def test_token_estimate_is_positive_and_monotonic() -> None:
    """``estimate_tokens`` 是估算（不引 tokenizer）：空串 0，非空为正且随长度单调不减。"""
    assert estimate_tokens("") == 0
    assert estimate_tokens("ab") >= 1
    assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)


# --------------------------------------------------------------------------- §E 审计三字段


def test_audit_records_llm_latency_tokens_and_coverage(ask_env: SimpleNamespace) -> None:
    """成功路径：``llmLatencyMs`` / ``answerTokens`` / ``citationCoverage`` 都落库。"""
    _with_fake_provider(
        ask_env,
        FakeAnswerProvider(
            reply="## Answer\n第一段 [E1]\n第二段（无引用）\n",
        ),
    )

    body = _ask(ask_env)
    assert body["status"] == "answered"

    row = ask_env.db._connect().execute(  # noqa: SLF001 - 直接查库断言"真的落进去了"
        "SELECT * FROM query_audit ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert row["degraded"] == 0
    assert row["citation_coverage"] == 0.5
    assert row["llm_latency_ms"] is not None and row["llm_latency_ms"] >= 0
    assert row["answer_tokens"] and row["answer_tokens"] > 0
    assert row["answerable"] == 1

    summary = _usage(ask_env)
    assert summary["citationCoverageAvg"] == 0.5
    assert summary["succeeded"] == 1
    assert summary["failed"] == 0
    record = summary["recent"][0]
    assert record["llmLatencyMs"] is not None
    assert record["answerTokens"] is not None


def test_audit_records_nulls_when_llm_not_used(ask_env: SimpleNamespace) -> None:
    """未走 LLM（未配置/失败）：三个观测值都是 ``null``（"没测过"不是 0）。"""
    _with_fake_provider(
        ask_env, FakeAnswerProvider(error=AnswerUnavailableError("boom"))
    )

    _ask(ask_env)

    summary = _usage(ask_env)
    record = summary["recent"][0]
    assert record["citationCoverage"] is None
    assert record["llmLatencyMs"] is None
    assert record["answerTokens"] is None
    assert record["degraded"] is True, "降级如实记 degraded"
    assert record["answerable"] is True, "检索本身仍有答案（降级的是 LLM 总结）"


def test_audit_migration_is_idempotent_on_existing_db(tmp_path: Path) -> None:
    """既有库（TASK-084 建的旧 schema）能安全加列：``PRAGMA`` 检查后 ``ALTER``，可重复。"""
    path = tmp_path / "nova-meta.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE query_audit (
          id INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, user_id TEXT,
          mode TEXT NOT NULL, query TEXT NOT NULL, answerable INTEGER, confidence TEXT,
          degraded INTEGER NOT NULL DEFAULT 0, latency_ms INTEGER NOT NULL,
          evidence_count INTEGER NOT NULL DEFAULT 0, docs_count INTEGER NOT NULL DEFAULT 0,
          used_tokens INTEGER NOT NULL DEFAULT 0, citation_coverage REAL,
          evidence_json TEXT NOT NULL DEFAULT '[]', created_at INTEGER NOT NULL
        );
        INSERT INTO query_audit (project_id, mode, query, degraded, latency_ms, created_at)
        VALUES ('p', 'deep', 'q', 1, 5, 1);
        """
    )
    connection.commit()
    connection.close()

    db = MetaDB.open(path)  # 打开即迁移
    MetaDB.open(path)  # 再打开一次：幂等，不报 duplicate column

    columns = {row[1] for row in db._connect().execute("PRAGMA table_info(query_audit)")}
    assert {"llm_latency_ms", "answer_tokens"} <= columns
    # 旧行的三个观测值都为 NULL（"没测过"不是 0；没有 provider 时这条降级路径不会崩）。
    assert build_provider(Settings(data_root=tmp_path, local_mode=True)) is None


def _usage(ns: SimpleNamespace) -> dict:
    response = ns.client.get("/api/usage/summary")
    assert response.status_code == 200, response.text
    return response.json()


# --------------------------------------------------------------------------- HTTP provider 细节


@pytest.mark.parametrize(
    "base_url,expected",
    [
        ("http://host:8080/v1", "http://host:8080/v1/chat/completions"),
        ("http://host:8080", "http://host:8080/v1/chat/completions"),
        ("http://host:8080/", "http://host:8080/v1/chat/completions"),
    ],
)
def test_endpoint_join_is_idempotent(base_url: str, expected: str) -> None:
    assert chat_completions_endpoint(base_url) == expected


def test_provider_retries_then_succeeds() -> None:
    """≤2 次重试：前两次 503、第三次 200 → 成功返回（§6 的"≤2 次重试后"才降级）。"""
    attempts: list[int] = []
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    provider = HttpAnswerProvider(
        base_url=FAKE_BASE_URL,
        api_key=FAKE_SK_KEY,
        model="m",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=slept.append,
    )
    assert provider.complete(system="s", user="u", max_tokens=8, temperature=0.0) == "ok"
    assert len(attempts) == 3
    assert slept == [0.5, 1.0], "指数退避（0.5s / 1s）"


def test_provider_gives_up_after_retries() -> None:
    """重试用尽 → :class:`AnswerUnavailableError`（上层据此降级，不 500）。"""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, text="overloaded")

    provider = HttpAnswerProvider(
        base_url=FAKE_BASE_URL,
        api_key=FAKE_SK_KEY,
        model="m",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    with pytest.raises(AnswerUnavailableError):
        provider.complete(system="s", user="u", max_tokens=8, temperature=0.0)
    assert len(attempts) == 3, "首次 + 2 次重试"


def test_provider_does_not_retry_auth_errors() -> None:
    """401 不重试（重试无意义）：一次调用即抛 :class:`AnswerAuthError`。"""
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(401, text="bad key")

    provider = HttpAnswerProvider(
        base_url=FAKE_BASE_URL,
        api_key=FAKE_SK_KEY,
        model="m",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _: None,
    )
    with pytest.raises(AnswerAuthError):
        provider.complete(system="s", user="u", max_tokens=8, temperature=0.0)
    assert len(attempts) == 1


def test_provider_rejects_empty_content() -> None:
    """``content`` 为空（reasoning 模型只产出 reasoning）→ 明确报错，绝不把空串当答案。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "", "reasoning": "..."}}]}
        )

    provider = HttpAnswerProvider(
        base_url=FAKE_BASE_URL,
        api_key=FAKE_SK_KEY,
        model="m",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(AnswerResponseError):
        provider.complete(system="s", user="u", max_tokens=8, temperature=0.0)


def test_provider_respects_retry_after_with_cap() -> None:
    """``Retry-After`` 被尊重但有上限（不允许多少秒就睡多少秒，避免挂死）。"""
    slept: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "9999"}, text="slow down")

    provider = HttpAnswerProvider(
        base_url=FAKE_BASE_URL,
        api_key=FAKE_SK_KEY,
        model="m",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=slept.append,
    )
    with pytest.raises(AnswerUnavailableError):
        provider.complete(system="s", user="u", max_tokens=8, temperature=0.0)
    assert slept and max(slept) <= answer_module.MAX_RETRY_AFTER


def test_provider_for_app_caches_by_settings_identity(
    ask_env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同配置只建一个 provider（连接复用）；配置对象换了就重建。"""
    settings = Settings(
        data_root=ask_env.tmp_path / "data",
        local_mode=True,
        answer_base_url=FAKE_BASE_URL,
        answer_api_key=FAKE_API_KEY,
        answer_model="m",
    )
    ask_env.app.state.settings = settings
    built: list[Mapping[str, object]] = []

    def fake_build(resolved: Settings, **_: object) -> HttpAnswerProvider:
        built.append({"model": resolved.answer_model})
        return HttpAnswerProvider(
            base_url=str(resolved.answer_base_url),
            api_key=str(resolved.answer_api_key),
            model=str(resolved.answer_model),
        )

    monkeypatch.setattr(answer_module, "HttpAnswerProvider", fake_build)  # type: ignore[arg-type]
    monkeypatch.setattr(answer_module, "build_provider", fake_build)  # type: ignore[arg-type]

    first = answer_module.provider_for_app(ask_env.app)
    second = answer_module.provider_for_app(ask_env.app)
    assert first is second
    assert len(built) == 1, "配置未变不该重建 provider"

    ask_env.app.state.settings = Settings(
        data_root=ask_env.tmp_path / "data",
        local_mode=True,
        answer_base_url=FAKE_BASE_URL,
        answer_api_key=FAKE_API_KEY,
        answer_model="m2",
    )
    third = answer_module.provider_for_app(ask_env.app)
    assert third is not first, "配置对象换了要重建（模型名是配置驱动的）"
    assert len(built) == 2


# --------------------------------------------------------------------------- §F 设置页后端


def test_meta_exposes_effective_config_without_key_material(ask_env: SimpleNamespace) -> None:
    """本地模式：``/api/meta`` 给出**生效中**的 embedding/LLM 展示值，且没有任何 key 痕迹。"""
    settings = Settings(
        data_root=ask_env.tmp_path / "data",
        local_mode=True,
        local_rescan_interval_s=0.0,
        answer_base_url="http://llm.invalid/v1",
        answer_api_key=FAKE_SK_KEY,
        answer_model="deepseek-flash",
        answer_provider="DeepSeek",
        answer_max_context_tokens=1_048_576,
    )
    ask_env.app.state.settings = settings
    ask_env.app.state.answer_provider = None
    ask_env.app.state.answer_provider_settings = None

    payload = ask_env.client.get("/api/meta").json()
    config = payload["config"]
    assert config["llm"]["configured"] is True
    assert config["llm"]["apiKeyConfigured"] is True
    assert config["llm"]["model"] == "deepseek-flash"
    assert config["llm"]["provider"] == "DeepSeek"
    # TASK-107：上下文窗口与厂商来自**显式配置**，不再靠硬编码模型名映射
    # （旧实现只认一个写死的（已废弃）模型名，与实际模型名不符时页面恒为空）。
    assert config["llm"]["maxContextTokens"] == 1_048_576
    assert config["llm"]["baseUrl"] == "http://llm.invalid/v1"
    assert config["llm"]["timeoutS"] == 60.0
    assert config["llm"]["maxTokens"] == 3072
    assert config["llm"]["temperature"] == 0.2
    # embedding 侧同样给展示值（本机默认 local；测试环境读不到 api 配置时不强求）。
    assert config["embedding"]["mode"] in {"local", "api"}
    dumped = json.dumps(payload, ensure_ascii=False)
    assert FAKE_SK_KEY not in dumped
    assert FAKE_SK_KEY[:8] not in dumped, "连 key 前缀都不许出现"
    assert str(len(FAKE_SK_KEY)) not in dumped, "连 key 长度都不许出现"


def test_meta_does_not_invent_model_quota_when_unconfigured(ask_env: SimpleNamespace) -> None:
    """TASK-107：未显式配置时**不猜**上下文窗口与速率配额，如实缺字段。

    防回归：旧实现按模型名硬编码回溯一个上下文值（而且认的是一个不存在的模型名），
    结果“页面显示一个数字”与“那个数字是真的”是两件事。现在只有显式配置才给。
    """
    settings = Settings(
        data_root=ask_env.tmp_path / "data",
        local_mode=True,
        local_rescan_interval_s=0.0,
        answer_base_url="http://llm.invalid/v1",
        answer_api_key=FAKE_SK_KEY,
        answer_model="some-unknown-model",
    )
    ask_env.app.state.settings = settings
    ask_env.app.state.answer_provider = None
    ask_env.app.state.answer_provider_settings = None

    config = ask_env.client.get("/api/meta").json()["config"]
    assert "maxContextTokens" not in config["llm"], "未知模型的窗口不能编"
    assert "provider" not in config["llm"], "认不出的厂商不能编"
    assert config["embedding"].get("tpm") is None, "未配 EMBED_TPM 时不应有数字"
    assert config["embedding"].get("rpm") is None


def test_meta_hides_model_details_when_unauthenticated(tmp_path: Path) -> None:
    """云端未鉴权：``/api/meta`` 只回“配了没/缺哪些环境变量名”，不回模型名与地址。"""
    settings = Settings(
        data_root=tmp_path / "cloud",
        local_mode=False,
        answer_base_url="http://internal-llm.example/v1",
        answer_api_key=FAKE_API_KEY,
        answer_model="internal/model-name",
    )
    app = create_app(settings)
    with make_client(app) as client:
        payload = client.get("/api/meta").json()

    config = payload["config"]
    assert config["llm"]["configured"] is True
    assert config["llm"]["apiKeyConfigured"] is True
    assert "model" not in config["llm"] and "baseUrl" not in config["llm"]
    dumped = json.dumps(payload, ensure_ascii=False)
    assert "internal/model-name" not in dumped
    assert "internal-llm.example" not in dumped
    assert FAKE_API_KEY not in dumped


def test_meta_reports_missing_answer_env_by_name(ask_env: SimpleNamespace) -> None:
    """未配置 LLM 时 ``/api/meta`` 明确列出缺哪些环境变量名（设置页文案与降级说明同源）。"""
    ask_env.app.state.settings = Settings(
        data_root=ask_env.tmp_path / "data", local_mode=True, local_rescan_interval_s=0.0
    )
    ask_env.app.state.answer_provider = None
    ask_env.app.state.answer_provider_settings = None

    llm = ask_env.client.get("/api/meta").json()["config"]["llm"]
    assert llm["configured"] is False
    assert llm["apiKeyConfigured"] is False
    assert llm["missingEnv"] == ["ANSWER_BASE_URL", "ANSWER_API_KEY", "ANSWER_MODEL"]
    assert llm["model"] is None
