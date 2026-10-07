"""跨模块 E2E 集成夹具（TASK-016 §D / R13：M1 级端到端回归的落点）。

被测链路**全部真实**：``Store``（SQLite + FTS5）、``VectorStore``（LanceDB）、``Indexer``
（真实解析 → 切块 → 二阶段解析 → 向量对账）、``retrieval``（三通道 + RRF + 图扩展 + rerank）、
``contextpack``。唯一的替身是 embedding provider。

假 provider（本文件定义，供 TASK-013/014 复用）——**确定性 bigram 哈希**：

- 特征 = jieba 预分词 token（与索引侧同一分词器）一元 / 二元 + token 串内字符 bigram；
- 特征名经 blake2b 哈希映射到固定维度桶、计数累加、L2 归一化；
- 因此"共享中文词的 query 与 chunk 在向量空间天然相近"，E2E 断言的失败只可能来自真实链路
  缺陷（或本算法的确定性缺陷），不会出现"假向量碰巧不相似"的假失败；CI 不联网、不加载模型。

语料（共享一个"令牌过期后刷新"的自然语言场景，代码块与设计文档块都覆盖查询词）：

- ``src/token_service.py``：``TokenService.refresh_token`` 是查询的目标符号；
- ``docs/design/token.md``：Markdown 设计文档（doctype=design，D-13/D-42 的一等资产）。
"""

from __future__ import annotations

import hashlib
import itertools
import math
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
from nova_core.contextpack import assemble
from nova_core.interfaces import EmbeddingProfile
from nova_core.pipeline import DirectorySource, Indexer, IngestReport
from nova_core.retrieval import (
    RecallResult,
    recall,
    recall_vector,
)
from nova_core.retrieval.expand import ExpansionResult, expand
from nova_core.retrieval.fusion import CHANNEL_VECTOR, merge
from nova_core.retrieval.rerank import collect_signals, rerank
from nova_core.storage import Store
from nova_core.text import segment
from nova_core.types import BlobInput, Candidate, ChangeSet, ContextPack
from nova_core.vectors import VectorStore

__all__ = [
    "DESIGN_DOC",
    "FAKE_DIM",
    "MODULE_PATH",
    "QUERY",
    "SPEC_PATH",
    "TARGET_SYMBOL",
    "TOKEN_MODULE",
    "DeterministicBigramEmbedding",
    "M1Env",
    "M1Run",
    "make_change_set",
    "write_repo",
]

#: 本文件的自然语言查询（R11 缺陷现场：分词后 ≥6 个 token，旧隐式 AND 恒零命中）。
QUERY = "令牌过期后在哪里刷新"
#: 查询的目标符号（BM25 通道应把它排在 top-3）。
TARGET_SYMBOL = "TokenService.refresh_token"
#: 语料路径。
MODULE_PATH = "src/token_service.py"
SPEC_PATH = "docs/design/token.md"

# 语料刻意**不含**查询原文（真实自然语言问题不会逐字出现在代码里）：没有任何单块同时含
# “令牌/过期/后/在/哪里/刷新”，因此旧隐式 AND 恒零命中——这正是 R11 的缺陷现场。
TOKEN_MODULE = '''"""令牌服务模块。"""


class TokenService:
    """令牌服务的入口。"""

    def refresh_token(self) -> str:
        """续期令牌：过期后由本方法负责刷新，签发细节见设计文档。"""
        return "old"

    def rotate(self) -> str:
        """轮换令牌，缓存未命中时回落数据库。"""
        return "rotate"
'''

DESIGN_DOC = """# 令牌设计

令牌过期时由 `refresh_token` 刷新。

## 刷新流程

令牌失效后如何续期：refresh_token 重新签发；缓存未命中时回落数据库。
"""

#: 假 provider 的向量维度（小维度让 LanceDB 与断言都轻量，且足以区分共享词的文本）。
FAKE_DIM = 64


def write_repo(root: Path, files: Mapping[str, str]) -> None:
    """按 ``path → 文本`` 落盘一个仓库（父目录自动创建）。"""
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def make_change_set(
    *,
    modified: Mapping[str, str] | None = None,
    deleted: Sequence[str] = (),
) -> ChangeSet:
    """按 ``path → 文本`` 构造 ``ChangeSet``（blob_hash 与 CF-02 口径一致）。"""

    def blob(path: str, content: str) -> BlobInput:
        data = content.encode("utf-8")
        return BlobInput(path=path, content=data, blob_hash=hashlib.sha256(data).hexdigest())

    return ChangeSet(
        modified=tuple(blob(path, content) for path, content in (modified or {}).items()),
        deleted=tuple(deleted),
    )


def _features(text: str) -> list[str]:
    """文本 → 特征名（jieba token 一元/二元 + token 串内字符 bigram）。"""
    tokens = [token for token in segment(text).split() if token]
    features = [f"t1:{token}" for token in tokens]
    features += [f"t2:{left}|{right}" for left, right in itertools.pairwise(tokens)]
    packed = "".join(tokens)
    features += [f"c2:{packed[index:index + 2]}" for index in range(len(packed) - 1)]
    return features


class DeterministicBigramEmbedding:
    """确定性假 ``EmbeddingProvider``：bigram 哈希桶 + L2 归一化（CI 不联网、不加载模型）。

    ``embed`` / ``embed_query`` 同一实现（无 query/passage 前缀差异），但两个方法都提供，
    以便断言"检索侧只走 ``embed_query``"这类契约（R2）。
    """

    def __init__(self, dim: int = FAKE_DIM) -> None:
        self._profile = EmbeddingProfile(
            model_id="fake:deterministic-bigram", dim=dim, max_input_tokens=512
        )

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        dim = self._profile.dim
        vector = [0.0] * dim
        for feature in _features(text):
            bucket = int.from_bytes(
                hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest(), "big"
            )
            vector[bucket % dim] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


@dataclass(frozen=True, slots=True)
class M1Run:
    """一次查询穿过全部五个模块后的中间结果（断言用）。"""

    recall: RecallResult
    expansion: ExpansionResult
    pool: list[Candidate]
    pack: ContextPack


@dataclass(slots=True)
class M1Env:
    """M1 端到端环境：真实 Store / VectorStore / Indexer + 假 embedding。"""

    repo: Path
    project: Path
    store: Store
    vectors: VectorStore
    indexer: Indexer
    provider: DeterministicBigramEmbedding

    def index_all(self) -> IngestReport:
        """全量索引仓库（``full_reparse``：解析 + 切块 + 嵌入 + 二阶段解析）。"""
        return self.indexer.full_reparse()

    def pipeline(self, query: str = QUERY) -> M1Run:
        """recall → expand → rerank → assemble（与 06-ContextEngine 的装配顺序一致）。"""
        result = recall(self.store, query, provider=self.provider, vector_store=self.vectors)
        expansion = expand(self.store, result.candidates)
        pool = [*result.candidates, *expansion.candidates]
        ranked = rerank(pool, collect_signals(self.store, query, pool))
        pack = assemble(self.store, query, ranked, flows=expansion.flows)
        return M1Run(recall=result, expansion=expansion, pool=pool, pack=pack)

    def vector_only_pack(self, query: str = QUERY) -> ContextPack:
        """**修复前反事实**：只用向量单通道的候选池（R11 时 BM25 通道恒空）→ 组装。

        仅用于断言"单通道不足以致 answerable"，作为回归护栏；不是生产路径。
        """
        vector_candidates = recall_vector(self.provider, self.vectors, query, limit=50)
        pool = merge({CHANNEL_VECTOR: vector_candidates}, store=self.store)
        return assemble(self.store, query, rerank(pool, collect_signals(self.store, query, pool)))


@pytest.fixture
def m1(tmp_path: Path) -> Iterator[M1Env]:
    """临时仓库 + 临时索引库（Store/VectorStore 都指向同一 project 目录）。"""
    repo = tmp_path / "repo"
    write_repo(repo, {MODULE_PATH: TOKEN_MODULE, SPEC_PATH: DESIGN_DOC})
    project = tmp_path / "project"
    provider = DeterministicBigramEmbedding()
    with (
        Store.open(project) as store,
        VectorStore.open(project, dim=provider.profile.dim) as vectors,
    ):
        yield M1Env(
            repo=repo,
            project=project,
            store=store,
            vectors=vectors,
            indexer=Indexer(store, provider, vectors, DirectorySource(repo)),
            provider=provider,
        )
