"""TASK-007 流水线测试夹具（本目录独占）。

两类替身（卡内"含 fake provider / 内存向量桩"）：

- :class:`CountingEmbedding`：确定性计数 provider（实现 ``EmbeddingProvider`` 协议），
  用来断言"只嵌了 N 个 chunk"；不依赖模型下载。
- 真实实现：``VectorStore``（LanceDB）与 ``LocalOnnxEmbeddingProvider``（注入微型 tokenizer +
  ONNX 会话替身）各跑一遍端到端（见 ``test_pipeline_real_stack.py``），
  对应卡内"合并前必须切真实实现跑一遍"。
"""

from __future__ import annotations

import hashlib
import pathlib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from nova_core.embedding import LocalOnnxEmbeddingProvider, get_local_spec
from nova_core.interfaces import EmbeddingProfile
from nova_core.pipeline import DirectorySource, Indexer
from nova_core.storage import Store
from nova_core.types import BlobInput, ChangeSet
from nova_core.vectors import VectorStore
from tokenizers import Tokenizer, models, pre_tokenizers, processors

#: 测试用 embedding 维度（小维度让 LanceDB 与断言都轻量）。
TEST_DIM = 16
TEST_PROFILE = EmbeddingProfile(
    model_id="local:test-hash", dim=TEST_DIM, max_input_tokens=512
)


class CountingEmbedding:
    """确定性哈希 embedding（每文本一个单位向量）+ 调用计数。"""

    def __init__(self, profile: EmbeddingProfile = TEST_PROFILE) -> None:
        self._profile = profile
        self.batches: list[list[str]] = []

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    @property
    def texts(self) -> list[str]:
        return [text for batch in self.batches for text in batch]

    @property
    def calls(self) -> int:
        return len(self.batches)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [_unit_vector(text, self._profile.dim) for text in texts]

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        return self.embed(texts)


def _unit_vector(text: str, dim: int) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vector = np.zeros(dim, dtype=np.float32)
    for index, byte in enumerate(digest[:dim]):
        vector[index] = (byte / 255.0) - 0.5
    norm = float(np.linalg.norm(vector))
    return [float(value) for value in (vector / norm if norm else vector)]


# ---------------------------------------------------------------------------
# 微型本地 ONNX provider（真实实现 + 替身会话，避免下载模型）
# ---------------------------------------------------------------------------

_TINY_VOCAB = {
    "[PAD]": 0,
    "[UNK]": 1,
    "[CLS]": 2,
    "[SEP]": 3,
    "hello": 4,
    "token": 5,
    "refresh": 6,
    "def": 7,
    "return": 8,
    "passage": 9,
    "query": 10,
    ":": 11,
}


class StubOnnxSession:
    """``onnxruntime.InferenceSession`` 替身：token id → one-hot hidden 状态。"""

    def __init__(self, dim: int) -> None:
        self.dim = dim
        self.calls = 0

    def get_inputs(self) -> list[Any]:
        from types import SimpleNamespace

        return [SimpleNamespace(name="input_ids"), SimpleNamespace(name="attention_mask")]

    def run(self, output_names: Any, input_feed: dict[str, np.ndarray]) -> list[np.ndarray]:
        self.calls += 1
        ids = np.asarray(input_feed["input_ids"])
        hidden = np.zeros((*ids.shape, self.dim), dtype=np.float32)
        for row in range(ids.shape[0]):
            for col in range(ids.shape[1]):
                hidden[row, col, int(ids[row, col]) % self.dim] = 1.0
        return [hidden]


@pytest.fixture
def tiny_tokenizer_path(tmp_path: pathlib.Path) -> Path:
    tokenizer = Tokenizer(models.WordLevel(vocab=_TINY_VOCAB, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.post_processor = processors.TemplateProcessing(
        single="[CLS] $A [SEP]",
        pair="[CLS] $A [SEP] $B:1 [SEP]:1",
        special_tokens=[("[CLS]", 2), ("[SEP]", 3)],
    )
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return path


@pytest.fixture
def local_provider(tmp_path: pathlib.Path, tiny_tokenizer_path: Path) -> LocalOnnxEmbeddingProvider:
    """真实 ``LocalOnnxEmbeddingProvider``（注入微型 tokenizer + stub 会话）。"""
    model_dir = tmp_path / "model"
    model_dir.mkdir(exist_ok=True)
    (model_dir / "tokenizer.json").write_bytes(tiny_tokenizer_path.read_bytes())
    spec = get_local_spec("multilingual-e5-small")
    return LocalOnnxEmbeddingProvider(
        spec, model_dir=model_dir, session=StubOnnxSession(dim=spec.dim)
    )


# ---------------------------------------------------------------------------
# 仓库夹具
# ---------------------------------------------------------------------------

PY_MODULE = '''"""小模块。"""

import os


def helper(value: int) -> int:
    """返回 +1。"""
    return value + 1


class Service:
    """服务。"""

    def run(self) -> int:
        return helper(1)
'''

DOC_MD = """# 设计

实现见 `helper` 与 `Service.run`。
"""


def write_repo(root: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


@pytest.fixture
def repo(tmp_path: pathlib.Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture
def store(tmp_path: pathlib.Path) -> Iterator[Store]:
    with Store.open(tmp_path / "proj") as opened:
        yield opened


@pytest.fixture
def vectors(tmp_path: pathlib.Path) -> Iterator[VectorStore]:
    with VectorStore.open(tmp_path / "proj", dim=TEST_DIM) as opened:
        yield opened


@pytest.fixture
def embedding() -> CountingEmbedding:
    return CountingEmbedding()


@pytest.fixture
def indexer(
    store: Store, embedding: CountingEmbedding, vectors: VectorStore, repo: Path
) -> Indexer:
    return Indexer(store, embedding, vectors, DirectorySource(repo))


@pytest.fixture
def change_set() -> Callable[..., ChangeSet]:
    """按 (path, 内容) 生成 ChangeSet（added/modified/deleted 可分别指定）。"""

    def _build(
        added: dict[str, str] | None = None,
        modified: dict[str, str] | None = None,
        deleted: Sequence[str] = (),
    ) -> ChangeSet:
        def blob(path: str, content: str) -> BlobInput:
            data = content.encode("utf-8")
            return BlobInput(path=path, content=data, blob_hash=hashlib.sha256(data).hexdigest())

        return ChangeSet(
            added=tuple(blob(path, text) for path, text in (added or {}).items()),
            modified=tuple(blob(path, text) for path, text in (modified or {}).items()),
            deleted=tuple(deleted),
        )

    return _build
