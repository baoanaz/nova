"""路径先验（retrieval/prior.py）：译文变体去重、变更日志与示例目录识别、查询意图。"""

from __future__ import annotations

from nova_core.retrieval.prior import (
    changelog_intent,
    example_intent,
    is_changelog_path,
    is_example_path,
    prefers_cjk,
    translation_key,
    translation_variants,
)
from nova_core.types import Candidate


def _cand(chunk_id: str, path: str, score: float) -> Candidate:
    return Candidate(chunk_id=chunk_id, kind="spec", rrf_score=score, score=score, path=path)


def test_translation_key_directory_and_filename_markers() -> None:
    assert translation_key("docs/zh/docs/tutorial/a.md") == ("docs/docs/tutorial/a.md", "zh")
    assert translation_key("docs/user-manual/ja/a.md") == ("docs/user-manual/a.md", "ja")
    assert translation_key("docs/release-notes/v3.15.0-zh.md") == (
        "docs/release-notes/v3.15.0.md",
        "zh",
    )
    assert translation_key("README.zh-CN.md") == ("README.md", "zh-cn")


def test_translation_key_ignores_source_code_directories() -> None:
    # 只对文档文件生效：源码目录恰好叫 de / id 不能被当成语言。
    assert translation_key("src/de/parser.py") is None
    assert translation_key("src/id/model.ts") is None
    assert translation_key("docs/guide.md") is None


def test_variants_keep_one_per_group_preferring_query_language() -> None:
    items = [
        _cand("en", "docs/en/docs/a.md", 0.9),
        _cand("zh", "docs/zh/docs/a.md", 0.5),
        _cand("ko", "docs/ko/docs/a.md", 0.8),
        _cand("other", "docs/en/docs/b.md", 0.4),
    ]
    # 英文查询：保留 en，其余译文降权；不相关文档不受影响。
    assert translation_variants(items, "how does it work") == {"zh", "ko"}
    # 中文查询：保留 zh（即使分更低）。
    assert translation_variants(items, "它是怎么工作的") == {"en", "ko"}


def test_variants_fall_back_to_best_scored_when_no_preferred_locale() -> None:
    items = [_cand("ko", "docs/ko/a.md", 0.3), _cand("fr", "docs/fr/a.md", 0.7)]
    assert translation_variants(items, "how") == {"ko"}


def test_variants_leave_single_file_and_unrelated_paths_alone() -> None:
    items = [
        _cand("a", "docs/en/a.md", 0.5),
        _cand("a2", "docs/en/a.md", 0.4),  # 同一文件的多个 chunk 不算重复
        _cand("b", "docs/guide.md", 0.6),
    ]
    assert translation_variants(items, "q") == frozenset()


def test_changelog_and_example_paths() -> None:
    assert is_changelog_path("docs/release-notes/v3.15.0-zh.md")
    assert is_changelog_path("CHANGELOG.md")
    assert not is_changelog_path("src/changelog.py")  # 只对文档文件生效
    assert is_example_path("examples/rpcbench/client.cc")
    assert is_example_path("docs_src/app_testing/tutorial003.py")
    assert not is_example_path("src/example_utils.py")


def test_intent_words_switch_the_penalties_off() -> None:
    assert changelog_intent("这个版本的更新日志里改了什么")
    assert changelog_intent("show me the release notes for 3.2")
    assert not changelog_intent("数据库 schema 版本号是多少")
    assert example_intent("给我一个使用示例")
    assert not example_intent("where is the cooldown defined")
    assert prefers_cjk("熔断器怎么工作") and not prefers_cjk("how does it work")
