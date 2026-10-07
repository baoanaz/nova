"""持久化 query 向量缓存（TASK-101 §F）：让基准测试**完全离线可复现**。

## 为什么需要它

向量通道在**查询时**才调 ``provider.embed_query(query)``（CF-09 的契约），因此只把索引
（``index.db`` + ``vectors/``）交给另一台主机是**不够的**——对方还要有 embedding key 或本地模型，
否则向量通道直接降级，跑出来的数字与出题机器不是同一个口径。

本模块把"查询 → 向量"这一层也固化进文件：预热一次（联网），之后任何主机只要带上这个文件，
就能在**无 key、无网络**的情况下跑出与出题机器完全一致的指标。

## 口径（必须与出题机器一致，否则对比无意义）

- **只缓存向量，不缓存检索结果**：候选池、rerank、装填全部照常现算，改检索代码后立刻可见；
- **TTL 不适用**：持久缓存永不过期（离线回放的全部意义就是"不重算"）；进程内的
  ``QueryEmbeddingCache`` 仍按 60s TTL 工作，两者互不冲突（前者是查表，后者是省往返）；
- **未命中 = 如实报错**：:class:`CachedOnlyProvider` 在缓存缺失时抛错，由检索链走"向量通道降级"
  并把用例计入报告的「向量通道降级用例数」。**不**静默换成 BM25 冒充命中——那会让指标口径失真。

## 文件格式（跨主机传递的就是它）

```json
{"schema": 1, "embedding_model": "api:voyage-4-lite", "dim": 1024,
 "queries": {"<query 原文>": [0.1, 0.2, ...]}}
```

``embedding_model`` / ``dim`` 是自证字段：与当前配置不一致时**拒绝使用**（否则拿别的模型的向量
去比 cosine，指标会莫名其妙地变差且无从察觉）。
"""

from __future__ import annotations

import json
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from nova_core.interfaces import EmbeddingProfile

__all__ = [
    "CACHE_SCHEMA",
    "PersistentQueryVectorCache",
    "CachedOnlyProvider",
]

#: 文件格式版本（不兼容变更时递增，旧文件按"未命中"处理而不是崩掉）。
CACHE_SCHEMA = 1


class PersistentQueryVectorCache:
    """``query → 向量`` 的 JSON 侧车文件（读写线程安全，原子落盘）。

    只实现 ``get`` / ``put`` 两个方法，因此可直接当作 ``recall(cache=...)`` 的缓存传入
    （``retrieval.vector`` 对该参数只做鸭子类型调用，不需要改任何签名）。
    """

    __slots__ = ("_path", "_model", "_dim", "_entries", "_lock", "_dirty", "hits", "misses")

    def __init__(
        self, path: str | Path, *, model: str | None = None, dim: int | None = None
    ) -> None:
        self._path = Path(path).expanduser()
        self._model = model
        self._dim = dim
        self._entries: dict[str, tuple[float, ...]] = {}
        self._lock = threading.RLock()
        self._dirty = False
        self.hits = 0
        self.misses = 0
        self.load()

    # ------------------------------------------------------------------ 读写

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> int:
        """读入侧车文件；不存在/损坏/版本或模型不符 → 当作空缓存（返回 0，不抛错）。

        为什么"不符就空"而不是报错：缓存是**加速手段**，不该让跑分起不来；真出问题会体现为
        未命中的清晰报错（见 :class:`CachedOnlyProvider`）。
        """
        if not self._path.is_file():
            return 0
        try:
            payload: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 0
        if not isinstance(payload, Mapping) or payload.get("schema") != CACHE_SCHEMA:
            return 0
        file_model = payload.get("embedding_model")
        file_dim = payload.get("dim")
        if self._model is not None and file_model not in (None, self._model):
            return 0
        if self._dim is not None and file_dim not in (None, self._dim):
            return 0
        # 采纳文件的自证字段（调用方没指定时）——否则"读到的模型"只用于当次校验就丢了，
        # 后续 identity_mismatch 会误判成"一致"（实测踩过：离线回放静默用了错维度）。
        if self._model is None and isinstance(file_model, str):
            self._model = file_model
        if self._dim is None and isinstance(file_dim, int):
            self._dim = file_dim
        queries = payload.get("queries")
        if not isinstance(queries, Mapping):
            return 0
        with self._lock:
            for query, vector in queries.items():
                if isinstance(vector, Sequence) and not isinstance(vector, (str, bytes)):
                    self._entries[str(query)] = tuple(float(value) for value in vector)
        return len(self._entries)

    def save(self) -> bool:
        """原子落盘（无变更则跳过）；返回是否写了文件。"""
        with self._lock:
            if not self._dirty and self._path.is_file():
                return False
            payload = {
                "schema": CACHE_SCHEMA,
                "embedding_model": self._model,
                "dim": self._dim,
                "queries": {query: list(vector) for query, vector in sorted(self._entries.items())},
            }
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(self._path.suffix + ".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n",
                encoding="utf-8",
            )
            temporary.replace(self._path)
            self._dirty = False
        return True

    def put_all(self) -> int:
        """落盘并返回当前条数（调用方在跑分结束后调一次）。

        只写真正取到过向量的 query：降级/短路用例不会留下占位条目——宁可下次仍然报
        "未命中"（可见、可定位），也不要让文件里出现无法区分真假的占位值。
        """
        self.save()
        with self._lock:
            return len(self._entries)

    def identity_mismatch(self, *, model: str, dim: int) -> str | None:
        """文件自证字段与给定 profile 不一致时返回可读原因（一致/未记录 → ``None``）。

        调用方（CLI）据此**拒绝使用**而不是照跑：拿别的模型的向量算 cosine，指标会莫名变差
        且无从察觉——这类"静默口径漂移"比直接报错危险得多。
        """
        if self._model is not None and self._model != model:
            return f"文件记录的 embedding 模型是 {self._model}"
        if self._dim is not None and self._dim != dim:
            return f"文件记录的向量维度是 {self._dim}"
        return None

    def bind_identity(self, *, model: str, dim: int) -> None:
        """把自证字段绑定成当前 profile（首次预热时文件里还没有这两个字段）。"""
        with self._lock:
            if self._model != model or self._dim != dim:
                self._model = model
                self._dim = dim
                self._dirty = True

    # ------------------------------------------------------------------ 缓存接口（recall 用）

    def get(self, query: str) -> list[float] | None:
        with self._lock:
            hit = self._entries.get(query)
            if hit is None:
                self.misses += 1
                return None
            self.hits += 1
            return list(hit)

    def put(self, query: str, vector: Sequence[float]) -> None:
        with self._lock:
            self._entries[query] = tuple(float(value) for value in vector)
            self._dirty = True

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


class CachedOnlyProvider:
    """离线 provider：**:only** 提供缓存里已有的 query 向量（TASK-101 §F）。

    用途：在**没有** embedding key / 本地模型的主机上复用别人预热好的侧车文件。语义上仍是
    "一个 embedding provider"，所以检索链一行都不用改；区别只在"取不到就抛错"。

    三条纪律：

    1. **绝不编造向量**：未命中直接抛 :class:`~nova_core.retrieval.vector.VectorChannelError`，
       检索链据此把该用例计入「向量通道降级」——指标口径的变化**可见**，不会被悄悄抹平；
    2. **索引侧一律拒绝**（``embed()``）：本 provider 只服务查询；如果有人拿它去建索引，
       应该立刻失败而不是写进一批"来自缓存"的假向量；
    3. ``profile`` 必须与出题机器一致（model/dim），否则那批向量根本不可比。
    """

    def __init__(
        self,
        cache: PersistentQueryVectorCache,
        *,
        model: str,
        dim: int,
    ) -> None:
        self._cache = cache
        #: ``max_input_tokens`` 不参与检索（只有索引侧截断才用它），给一个占位值即可。
        self._profile = EmbeddingProfile(model_id=model, dim=dim, max_input_tokens=2048)

    @property
    def profile(self) -> EmbeddingProfile:
        return self._profile

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError(
            "CachedOnlyProvider 只服务查询（离线回放）；索引侧请用真实 provider。"
        )

    def embed_query(self, texts: Sequence[str]) -> list[list[float]]:
        from nova_core.retrieval.vector import VectorChannelError

        vectors: list[list[float]] = []
        for text in texts:
            cached = self._cache.get(text)
            if cached is None:
                raise VectorChannelError(
                    f"离线向量缓存未命中：{text[:60]!r} —— 该 query 未包含在随附的向量侧车文件里；"
                    "请在能联网/有 key 的机器上用 --vector-cache 预热后重新分发。"
                )
            vectors.append(cached)
        return vectors
