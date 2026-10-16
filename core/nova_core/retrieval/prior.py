"""路径先验：只看路径与查询文字、不依赖索引内容的排序信号。

来自 benchmark 基线（fastapi / cc-switch / gkd 等）暴露的三类系统性噪声：

1. **译文变体**：同一篇文档的多语言版本（``docs/zh/...``、``docs/ko/...``、``v3.15-ja.md``）
   内容几乎相同，会一起占满前 10 名，把真正的实现挤出去；
2. **变更日志 / 发布说明**：提到大量功能名与版本号，对「某功能在哪里实现」几乎没有帮助；
3. **示例 / 教程目录**：例子里到处是同名的变量与调用，排名常压过核心实现。

三者都按「查询没有明确要找它」才降权，用户真想看发布说明或示例时不受影响。
所有判定是确定性的路径规则，不调用任何模型。
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from nova_core.types import Candidate

__all__ = [
    "changelog_intent",
    "example_intent",
    "is_changelog_path",
    "is_example_path",
    "prefers_cjk",
    "translation_key",
    "translation_variants",
]

_DOC_EXT = (".md", ".mdx", ".rst", ".txt", ".adoc")

#: 语言代码（小写）。仅在文档文件里识别，避免把 ``src/de/``、``id`` 之类的源码目录当成语言。
_LOCALES = frozenset(
    "en zh zh-cn zh-tw zh-hans zh-hant zh_cn zh_tw ja ko de fr es pt pt-br pt_br ru tr hi it vi id "
    "pl uk ar nl sv th he fa ro cs el hu bg da fi no sk".split()
)
_ZH_FAMILY = frozenset(locale for locale in _LOCALES if locale.startswith("zh"))

_LOCALE_ALT = "|".join(sorted((re.escape(x) for x in _LOCALES), key=len, reverse=True))
#: 文件名里的语言标记：``-zh.md``、``_ja.md``、``.zh-CN.md``。
_FILE_MARK_RE = re.compile(rf"([-_.])({_LOCALE_ALT})(?=\.[A-Za-z0-9]+$)", re.IGNORECASE)

_CHANGELOG_DIRS = frozenset(
    {"release-notes", "release_notes", "releasenotes", "releases", "changelog", "changelogs"}
)
_CHANGELOG_NAME_RE = re.compile(
    r"^(changelog|changes|history|news|release[-_ ]?notes?)", re.IGNORECASE
)
_EXAMPLE_DIRS = frozenset(
    {
        "example",
        "examples",
        "sample",
        "samples",
        "demo",
        "demos",
        "docs_src",
        "tutorial",
        "tutorials",
        "cookbook",
        "snippets",
    }
)

_CHANGELOG_WORDS = (
    "changelog",
    "change log",
    "release note",
    "what's new",
    "whats new",
    "更新日志",
    "变更日志",
    "发布说明",
    "更新记录",
    "版本记录",
    "更新说明",
)
_EXAMPLE_WORDS = (
    "example",
    "sample",
    "demo",
    "tutorial",
    "cookbook",
    "snippet",
    "示例",
    "样例",
    "例子",
    "教程",
    "演示",
)
_CJK_RE = re.compile(r"[㐀-鿿]")


def _is_doc(path: str) -> bool:
    return path.lower().endswith(_DOC_EXT)


def _segments(path: str) -> list[str]:
    return path.replace("\\", "/").lower().split("/")


def is_changelog_path(path: str) -> bool:
    if not _is_doc(path):
        return False
    segments = _segments(path)
    in_changelog_dir = any(s in _CHANGELOG_DIRS for s in segments[:-1])
    return in_changelog_dir or bool(_CHANGELOG_NAME_RE.match(segments[-1]))


def is_example_path(path: str) -> bool:
    return any(s in _EXAMPLE_DIRS for s in _segments(path)[:-1])


def changelog_intent(query: str) -> bool:
    lowered = query.lower()
    return any(word in lowered for word in _CHANGELOG_WORDS)


def example_intent(query: str) -> bool:
    lowered = query.lower()
    return any(word in lowered for word in _EXAMPLE_WORDS)


def prefers_cjk(query: str) -> bool:
    return bool(_CJK_RE.search(query))


def translation_key(path: str) -> tuple[str, str | None] | None:
    """文档路径 → ``(去掉语言标记后的归一路径, 语言)``；不含语言标记返回 ``None``。

    语言标记可以是目录段（``docs/zh/docs/a.md``、``docs/user-manual/ja/a.md``）或文件名后缀
    （``v1-zh.md``、``README.zh-CN.md``）。只对文档文件生效。
    """
    if not _is_doc(path):
        return None
    segments = path.replace("\\", "/").split("/")
    for index, segment in enumerate(segments[:-1]):
        if segment.lower() in _LOCALES:
            rest = [s for i, s in enumerate(segments) if i != index]
            return "/".join(rest), segment.lower()
    match = _FILE_MARK_RE.search(segments[-1])
    if match:
        name = segments[-1][: match.start()] + segments[-1][match.end() :]
        return "/".join([*segments[:-1], name]), match.group(2).lower()
    return None


def _is_preferred(locale: str | None, cjk: bool) -> bool:
    if locale is None:
        return not cjk
    return locale in _ZH_FAMILY if cjk else locale == "en"


def translation_variants(candidates: Iterable[Candidate], query: str) -> frozenset[str]:
    """同一文档的多语言变体里，需要降权的 chunk id。

    每组只保留一个文件：优先与查询语言一致的版本（中文查询取 zh，其余取 en 或无标记原文），
    没有就保留组内当前分最高的；其余文件的所有 chunk 都在返回集合里。
    """
    cjk = prefers_cjk(query)
    groups: dict[str, dict[str, list[Candidate]]] = {}
    for candidate in candidates:
        if not candidate.path:
            continue
        marked = translation_key(candidate.path)
        # 无标记的原文也要参与分组：它和带标记的译文是同一篇。
        key = marked[0] if marked else candidate.path
        groups.setdefault(key, {}).setdefault(candidate.path, []).append(candidate)

    demoted: set[str] = set()
    for variants in groups.values():
        if len(variants) >= 2:
            demoted.update(_demoted_in_group(variants, cjk))
    return frozenset(demoted)


def _demoted_in_group(variants: dict[str, list[Candidate]], cjk: bool) -> set[str]:
    def locale_of(path: str) -> str | None:
        marked = translation_key(path)
        return marked[1] if marked else None

    def best(path: str) -> float:
        return max(c.score for c in variants[path])

    preferred = [p for p in variants if _is_preferred(locale_of(p), cjk)]
    keeper = max(preferred or list(variants), key=best)
    return {c.chunk_id for path, chunks in variants.items() if path != keeper for c in chunks}
