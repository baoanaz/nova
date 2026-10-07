"""真实实现端到端：``LocalOnnxEmbeddingProvider``（TASK-008）+ ``VectorStore``（TASK-009）。

任务卡要求"合并前必须切真实实现跑一遍"：本文件不使用计数替身，只注入微型 tokenizer 与会话替身
（避免下载模型），其余全走生产路径（真 Store / 真 LanceDB / 真 provider 内部批处理与归一化）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from nova_core.embedding import LocalOnnxEmbeddingProvider
from nova_core.pipeline import DirectorySource, Indexer
from nova_core.storage import Store
from nova_core.types import ChangeSet
from nova_core.vectors import VectorStore

from .conftest import DOC_MD, PY_MODULE, write_repo

ChangeSetFactory = Callable[..., ChangeSet]


def test_end_to_end_with_real_stack(
    tmp_path: Path,
    repo: Path,
    local_provider: LocalOnnxEmbeddingProvider,
    change_set: ChangeSetFactory,
) -> None:
    write_repo(repo, {"pkg/mod.py": PY_MODULE, "docs/design.md": DOC_MD})
    with Store.open(tmp_path / "proj") as store:
        with VectorStore.open(tmp_path / "proj", dim=local_provider.profile.dim) as vectors:
            indexer = Indexer(store, local_provider, vectors, DirectorySource(repo))
            report = indexer.ingest(
                change_set(
                    added={"pkg/mod.py": PY_MODULE, "docs/design.md": DOC_MD}
                )
            )
            counts = store.counts()

            assert report.added == 2
            assert counts["chunks"] == report.chunks_new == vectors.count()
            assert report.vectors_upserted == counts["chunks"]
            assert report.languages == ("markdown", "python")

            # 真实向量检索：命中的 chunk_id 必须都能在 SQLite 侧取回（TASK-010 的消费前提）
            query_vector = local_provider.embed_query(["helper 返回 value"])[0]
            hits = vectors.search(query_vector, top_k=5)
            assert hits
            assert store.chunks_by_ids([hit.chunk_id for hit in hits])

            # 增量：改 1 个函数 → 只有该 chunk 的向量被重写
            updated = PY_MODULE.replace("return value + 1", "return value + 41")
            incremental = indexer.ingest(change_set(modified={"pkg/mod.py": updated}))

            assert incremental.chunks_new == 1
            assert incremental.vectors_upserted == 1
            assert vectors.count() == store.counts()["chunks"]

            # 幂等：同一变更集二次 ingest 零写入
            again = indexer.ingest(change_set(modified={"pkg/mod.py": updated}))

            assert again.chunks_new == 0
            assert again.vectors_upserted == 0
            assert vectors.count() == store.counts()["chunks"]

            # 删除：索引行、向量、spec 引用级联一起收敛
            after_delete = indexer.ingest(change_set(deleted=["pkg/mod.py"]))

            assert after_delete.deleted == 1
            assert store.counts()["files"] == 1
            assert vectors.count() == store.counts()["chunks"]
