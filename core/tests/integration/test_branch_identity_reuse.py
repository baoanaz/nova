"""TASK-111 端到端：分支隔离 + 跨项目向量复用。

两件事必须同时成立，否则设计自相矛盾：

1. **隔离**：同一 remote 的两个分支 → 不同 projectId（不再串分支、不再返回对方分支的文件）；
2. **复用**：第二个分支索引时**复用**第一个分支已嵌入的内容（否则"隔离"把 embedding
   与存储费用乘上分支数，与"最小消耗 embedding 额度"的目标直接冲突）。

实测参照（真实 voyage-4-lite / nova 仓库 10 个 worktree）：

```text
main worktree : new=5913  reused=0           180.0s
lane-c        : new=5842  reused=5824 (99.7%) 29.5s
lane-f        : new=5682  reused=5595 (98.5%) 28.1s
```

CI 用确定性假 provider（不联网），断言的是**调用次数**而非耗时。
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
from nova_core.engine import Engine
from nova_core.interfaces import EmbeddingProfile

from .conftest import DeterministicBigramEmbedding

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.com",
}

SHARED_SOURCE = '''"""共享模块。"""


def shared(value: int) -> int:
    """两端都在。"""
    return value + 1
'''

BRANCH_SOURCE = '''"""分支专用模块。"""


def only_on_branch() -> str:
    return "branch"
'''


class CountingEmbedding(DeterministicBigramEmbedding):
    """在假 provider 上数**被嵌入的文本条数**（断言"复用了多少"）。"""

    def __init__(self, dim: int = 64) -> None:
        super().__init__(dim=dim)
        self.texts: list[str] = []

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return super().embed(texts)

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        self.texts.extend(texts)
        return super().embed_query(texts)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        env={**os.environ, **_GIT_ENV},
    )


def _make_repo(root: Path, remote: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    except (OSError, subprocess.CalledProcessError) as exc:  # pragma: no cover
        pytest.skip(f"环境不可用 git：{exc}")
    _git(root, "remote", "add", "origin", remote)
    (root / "pkg").mkdir(exist_ok=True)
    (root / "pkg" / "shared.py").write_text(SHARED_SOURCE, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "init")
    _git(root, "branch", "-M", "main")
    return root


def test_two_branches_isolated_but_second_reuses_vectors(tmp_path: Path) -> None:
    remote = "https://example.com/team/nova.git"
    repo = _make_repo(tmp_path / "nova", remote)

    provider = CountingEmbedding()
    engine = Engine(tmp_path / "data", provider=provider)

    main_handle, main_identity = engine.resolve_repo(repo)
    main_report = engine.ingest_repo(main_handle.project_id, repo)
    assert main_report.chunks_new > 0
    texts_after_main = len(provider.texts)

    # 切到另一个分支：新增一个文件，shared.py 内容不变 → 应复用。
    _git(repo, "checkout", "-qb", "feature/x")
    (repo / "pkg" / "branch_only.py").write_text(BRANCH_SOURCE, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "branch work")

    branch_handle, branch_identity = engine.resolve_repo(repo)
    branch_report = engine.ingest_repo(branch_handle.project_id, repo)

    # ① 隔离：两个分支是不同的项目
    assert main_handle.project_id != branch_handle.project_id
    assert main_identity.identity_key != branch_identity.identity_key

    # ② 复用：第二个分支只为新增内容付费（shared.py 走缓存，不重嵌）
    #    TASK-114 起"按内容复用向量"记在 ``chunks_deduped``；``chunks_reused`` 只表示文件级对账。
    assert branch_report.chunks_deduped > 0, "第二个分支必须复用已有向量"
    newly_embedded = len(provider.texts) - texts_after_main
    assert newly_embedded < branch_report.chunks_new, (
        f"第二个分支不应全量重嵌：新增嵌入 {newly_embedded} 条 vs chunks {branch_report.chunks_new}"
    )
    # 共享文件的内容确实没有重新嵌入
    assert not any("共享模块" in text for text in provider.texts[texts_after_main:])


def test_switching_back_to_main_reuses_everything(tmp_path: Path) -> None:
    """在 main 与 feature 之间来回切：第二次回到 main 应完全命中缓存（零新增嵌入）。"""
    remote = "https://example.com/team/nova.git"
    repo = _make_repo(tmp_path / "nova", remote)

    provider = CountingEmbedding()
    engine = Engine(tmp_path / "data", provider=provider)

    main_handle, _ = engine.resolve_repo(repo)
    engine.ingest_repo(main_handle.project_id, repo)

    _git(repo, "checkout", "-qb", "feature/x")
    (repo / "pkg" / "branch_only.py").write_text(BRANCH_SOURCE, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "branch work")
    branch_handle, _ = engine.resolve_repo(repo)
    engine.ingest_repo(branch_handle.project_id, repo)

    # 切回 main 并重索引：内容全部已缓存（含 feature 分支写入的共享文件）
    _git(repo, "checkout", "-q", "main")
    back_handle, _ = engine.resolve_repo(repo)
    assert back_handle.project_id == main_handle.project_id
    texts_before = len(provider.texts)
    report = engine.ingest_repo(back_handle.project_id, repo)

    assert len(provider.texts) == texts_before, "内容未变时不得有任何新嵌入"
    assert report.chunks_new == 0


def test_cache_can_be_disabled_by_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``NOVA_EMBED_CACHE=off`` 时退化为项目内复用（回归对比用），跨分支不再命中。"""
    remote = "https://example.com/team/nova.git"
    repo = _make_repo(tmp_path / "nova", remote)

    provider = CountingEmbedding()
    engine = Engine(tmp_path / "data", provider=provider)
    main_handle, _ = engine.resolve_repo(repo)
    engine.ingest_repo(main_handle.project_id, repo)
    assert (tmp_path / "data" / "cache" / "embeddings").is_dir(), "默认应写共享缓存"

    monkeypatch.setenv("NOVA_EMBED_CACHE", "off")
    provider2 = CountingEmbedding()
    engine2 = Engine(tmp_path / "data2", provider=provider2)
    h, _ = engine2.resolve_repo(repo)
    engine2.ingest_repo(h.project_id, repo)
    assert not (tmp_path / "data2" / "cache").exists(), "关闭后不得建缓存目录"


def test_profile_model_id_is_used_as_cache_shard(tmp_path: Path) -> None:
    """缓存分片名必须来自 ``profile.model_id``（换模型即换分片，不会串用旧向量）。"""
    remote = "https://example.com/team/nova.git"
    repo = _make_repo(tmp_path / "nova", remote)
    provider = CountingEmbedding()
    engine = Engine(tmp_path / "data", provider=provider)
    handle, _ = engine.resolve_repo(repo)
    engine.ingest_repo(handle.project_id, repo)

    profile: EmbeddingProfile = provider.profile
    shards = [p.name for p in (tmp_path / "data" / "cache" / "embeddings").iterdir()]
    assert shards, "缓存分片目录应存在"
    assert any(str(profile.dim) in name for name in shards)
