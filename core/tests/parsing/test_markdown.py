"""TASK-005 验收测试：Markdown SpecBlock 抽取器。

覆盖：标题树/块边界、同名标题消歧、中文混排、代码围栏行号与围栏内 # 不产生标题、
front matter、setext 口径、前言块、doctype 规则表、mentioned 提取、确定性、dogfood 冒烟。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from nova_core.parsing.markdown import MarkdownParser, classify_doctype, spec_block_id
from nova_core.types import ParsedFile, SpecBlockDef

SAMPLE_DIR = Path(__file__).parent / "samples" / "markdown"
REPO_ROOT = Path(__file__).resolve().parents[3]
PARSER = MarkdownParser()


def _parse_sample(name: str) -> ParsedFile:
    text = (SAMPLE_DIR / name).read_text(encoding="utf-8")
    return PARSER.parse(name, text)


def _by_heading(parsed: ParsedFile, heading: str) -> list[SpecBlockDef]:
    return [block for block in parsed.spec_blocks if block.heading == heading]


def test_sample_corpus_has_at_least_six_files() -> None:
    assert len(list(SAMPLE_DIR.rglob("*.md"))) >= 6


def test_heading_tree_paths_and_block_boundaries() -> None:
    parsed = _parse_sample("nested.md")

    token = _by_heading(parsed, "token 刷新流程")
    assert len(token) == 1
    assert token[0].level == 3
    assert token[0].heading_path == "架构 > 认证模块 > token 刷新流程"
    assert token[0].start_line == 9

    session = _by_heading(parsed, "会话管理")
    assert session[0].heading_path == "架构 > 认证模块 > 会话管理"
    # 块边界：到下一个同级标题（会话管理）之前为止
    assert token[0].end_line == session[0].start_line - 1 == 18

    parent = _by_heading(parsed, "认证模块")
    # 同名标题：第一个 "认证模块" 到第二个同级标题前为止
    assert parent[0].start_line == 5
    assert parent[0].end_line == parent[1].start_line - 1 == 26

    root = _by_heading(parsed, "架构")
    assert (root[0].start_line, root[0].end_line) == (1, 33)
    # 父块 content 覆盖完整子树
    assert "token 刷新流程" in root[0].content
    assert "plain fence without language" in root[0].content


def test_duplicate_headings_get_distinct_ids() -> None:
    parsed = _parse_sample("nested.md")
    duplicates = _by_heading(parsed, "认证模块")
    assert len(duplicates) == 2

    ids = [spec_block_id(block) for block in duplicates]
    assert len(set(ids)) == 2
    assert ids[0] == f"nested.md:架构 > 认证模块:{duplicates[0].start_line}"
    assert ids[1] == f"nested.md:架构 > 认证模块:{duplicates[1].start_line}"
    assert ids[0] != ids[1]


def test_chinese_heading_and_mixed_content() -> None:
    parsed = _parse_sample("nested.md")
    token = _by_heading(parsed, "token 刷新流程")[0]
    assert "刷新流程如下" in token.content
    assert "def refresh_token(service):" in token.content
    assert token.doctype == "guide"


def test_code_fences_line_numbers_and_langs() -> None:
    parsed = _parse_sample("nested.md")

    token = _by_heading(parsed, "token 刷新流程")[0]
    assert len(token.code_fences) == 1
    fence = token.code_fences[0]
    assert (fence.lang, fence.line) == ("python", 13)
    assert fence.content.splitlines()[0] == "def refresh_token(service):"
    assert "# 这是围栏内的注释，不是标题" in fence.content

    session = _by_heading(parsed, "会话管理")[0]
    assert len(session.code_fences) == 1
    assert (session.code_fences[0].lang, session.code_fences[0].line) == ("", 23)
    assert session.code_fences[0].content == "plain fence without language"

    # 围栏归属最内层块：父块不重复登记
    assert all(not block.code_fences for block in _by_heading(parsed, "架构"))
    owners = [block.heading for block in parsed.spec_blocks if block.code_fences]
    assert owners == ["token 刷新流程", "会话管理", "认证模块"]
    assert [block.code_fences[0].line for block in parsed.spec_blocks if block.code_fences] == [
        13,
        23,
        31,
    ]

    # 围栏内的 "# comment" / "# 围栏内的伪标题" 不产生标题
    assert not any(block.heading.startswith("#") for block in parsed.spec_blocks)
    assert not any("围栏内的伪标题" in block.heading for block in parsed.spec_blocks)
    assert all(block.heading != "围栏内的注释，不是标题" for block in parsed.spec_blocks)


def test_front_matter_is_a_standalone_block() -> None:
    parsed = _parse_sample("frontmatter.md")

    front = [block for block in parsed.spec_blocks if block.heading_path == "(front matter)"]
    assert len(front) == 1
    assert front[0].level == 0
    assert (front[0].start_line, front[0].end_line) == (1, 4)
    assert front[0].content == "title: 认证设计\ntags: [token, session]"
    assert parsed.spec_blocks[0] is front[0]

    assert [block.heading_path for block in parsed.spec_blocks] == [
        "(front matter)",
        "认证设计",
        "认证设计 > 刷新时机",
    ]


def test_preamble_block_and_setext_is_body_text() -> None:
    parsed = _parse_sample("setext-and-preamble.md")

    # setext 标题不识别：不产生对应块，按正文处理
    assert not _by_heading(parsed, "Setext 标题")

    preamble = parsed.spec_blocks[0]
    assert preamble.heading_path == "(preamble)"
    assert (preamble.start_line, preamble.end_line) == (1, 9)
    assert "Setext 标题" in preamble.content

    assert [block.heading_path for block in parsed.spec_blocks[1:]] == ["ATX 小节"]


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("AGENTS.md", "agent-instructions"),
        (".cursorrules", "agent-instructions"),
        # TASK-097 §C：白名单默认清单里的另外几个名字（只扩名表，不改 rerank 分值）
        ("CLAUDE.md", "agent-instructions"),
        (".agent.md", "agent-instructions"),
        ("HANDOFF.md", "agent-instructions"),
        ("docs/design/HANDOFF.md", "agent-instructions"),  # 名表先于路径规则（首命中生效）
        ("skills/learned/SKILL.md", "agent-instructions"),
        ("README.md", "readme"),
        ("docs/design/x.md", "design"),
        ("ARCHITECTURE.md", "design"),
        ("docs/adr/y.md", "adr"),
        ("API.md", "api"),
        ("docs/PROTOCOL.md", "api"),
        ("CHANGELOG.md", "changelog"),
        ("docs/guides/setup.md", "guide"),
    ],
)
def test_doctype_rule_table(path: str, expected: str) -> None:
    assert classify_doctype(path) == expected


def test_doctype_applied_to_spec_blocks() -> None:
    for name, expected in [
        ("AGENTS.md", "agent-instructions"),
        ("README.md", "readme"),
        ("docs/design/x.md", "design"),
        ("docs/adr/y.md", "adr"),
        ("CHANGELOG.md", "changelog"),
    ]:
        parsed = _parse_sample(name)
        assert parsed.spec_blocks, name
        assert {block.doctype for block in parsed.spec_blocks} == {expected}


def test_mentioned_extraction() -> None:
    parsed = _parse_sample("frontmatter.md")
    root = _by_heading(parsed, "认证设计")[0]
    # 行内 code（含 A::b 形态）与带扩展名路径
    assert "TokenService::refresh" in root.mentioned
    assert "core/nova_core/types.py" in root.mentioned

    nested = _parse_sample("nested.md")
    root = _by_heading(nested, "架构")[0]
    assert "TokenService" in root.mentioned  # camelCase
    assert "refresh_token" in root.mentioned  # snake_case（行内 code + 围栏代码）
    assert "SessionStore" in root.mentioned

    # 去重且保持顺序稳定
    for parsed_file in (parsed, nested):
        for block in parsed_file.spec_blocks:
            assert len(block.mentioned) == len(set(block.mentioned))


def test_deterministic_same_input_twice() -> None:
    for name in ["nested.md", "frontmatter.md", "setext-and-preamble.md"]:
        text = (SAMPLE_DIR / name).read_text(encoding="utf-8")
        assert PARSER.parse(name, text) == PARSER.parse(name, text)


def test_parser_contract_never_raises() -> None:
    assert PARSER.parse("empty.md", "") == ParsedFile(path="empty.md", language="markdown")

    unclosed = PARSER.parse("unclosed.md", "```python\nprint(1)\n")
    assert unclosed.fallback is False
    preamble = unclosed.spec_blocks[0]
    assert preamble.heading_path == "(preamble)"
    assert (preamble.code_fences[0].lang, preamble.code_fences[0].line) == ("python", 1)
    assert preamble.code_fences[0].content == "print(1)"

    assert PARSER.language == "markdown"


def test_dogfood_smoke_on_module_design_doc() -> None:
    rel_path = "docs/design/Module/01-切片存储.md"
    text = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
    parsed = PARSER.parse(rel_path, text)

    assert parsed.fallback is False
    assert len(parsed.spec_blocks) > 10
    assert any("2.2" in block.heading_path for block in parsed.spec_blocks)
    assert {block.doctype for block in parsed.spec_blocks} == {"design"}

    ids = [spec_block_id(block) for block in parsed.spec_blocks]
    assert len(ids) == len(set(ids))
    for block in parsed.spec_blocks:
        assert 1 <= block.start_line <= block.end_line <= len(text.splitlines())
