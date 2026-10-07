"""图解析的内存符号索引必须与原实现逐项等价。

- ``_SymbolIndex.by_name``（一次读全表的内存索引）≡ ``Store.exact_symbols(name, limit=None)``
  （集合与顺序都相同，含 NULL ``file_path`` / ``start_line`` 的排序口径）；
"""

from __future__ import annotations

from pathlib import Path

from nova_core.chunking.resolver import _SymbolIndex
from nova_core.hashing import blob_hash
from nova_core.interfaces import EmbeddingProfile
from nova_core.pipeline import DirectorySource, Indexer
from nova_core.storage import Store
from nova_core.types import BlobInput, ChangeSet
from nova_core.vectors import VectorStore

_REPO_ROOT = Path(__file__).resolve().parents[3]
TEST_DIM = 8


class CountingEmbedding:
    """最小确定性 embedding 替身（图解析与向量无关，只需能跑完 ingest）。"""

    profile = EmbeddingProfile(model_id="test:resolver", dim=TEST_DIM, max_input_tokens=512)

    def embed(self, texts):  # noqa: ANN001, ANN201
        return [[1.0] + [0.0] * (TEST_DIM - 1) for _ in texts]

    def embed_query(self, texts):  # noqa: ANN001, ANN201
        return self.embed(texts)


def _indexed_store(tmp_path: Path) -> Store:
    repo = tmp_path / "repo"
    blobs = []
    for source in sorted((_REPO_ROOT / "core" / "nova_core").rglob("*.py")):
        path = source.relative_to(_REPO_ROOT).as_posix()
        data = source.read_bytes()
        blobs.append(BlobInput(path=path, content=data, blob_hash=blob_hash(path, data)))
    store = Store.open(tmp_path / "proj")
    with VectorStore.open(tmp_path / "proj", dim=TEST_DIM) as vectors:
        Indexer(store, CountingEmbedding(), vectors, DirectorySource(repo), workers=1).ingest(
            ChangeSet(added=tuple(blobs))
        )
    return store


def test_symbol_index_matches_exact_symbols(tmp_path: Path) -> None:
    store = _indexed_store(tmp_path)
    try:
        conn = store._conn  # noqa: SLF001 - 构造 NULL 排序边界
        conn.executemany(
            "INSERT INTO symbols(id, name, fqn, kind, chunk_id, file_path, start_line,"
            " end_line, is_exported) VALUES(?, ?, ?, 'function', NULL, ?, ?, NULL, ?)",
            [
                ("z:null-path", "Engine", "x.Engine", None, 5, 1),
                ("a:null-line", "Engine", "y.Engine", "a.py", None, 1),
                ("m:both-null", "Engine", "Engine", None, None, 0),
                ("é:unicode", "Engine", "é.Engine", "é.py", 1, 1),
            ],
        )
        conn.commit()
        names = {row[0] for row in conn.execute("SELECT name FROM symbols")}
        names |= {row[0] for row in conn.execute("SELECT fqn FROM symbols")}
        names |= {"no-such-symbol", ""}
        assert len(names) > 500
        index = _SymbolIndex(store)
        for name in sorted(names):
            assert index.by_name(name) == store.exact_symbols(name, limit=None), name
    finally:
        store.close()
