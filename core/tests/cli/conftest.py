"""TASK-013 CLI / engine 测试夹具（本目录独占）。

三个夹具角色：

- :class:`CountingEmbedding`：确定性哈希 embedding（计数调用与文本），替换真实 ONNX 模型，
  让 ``ingest → search`` 端到端（真实 Store + VectorStore + 解析器 + 检索 + 组装）可离线复现；
- ``repo``：fixture 小仓库（Python 模块 + 设计文档），覆盖符号/文档两类切片；
- ``run_cli``：以 fake provider 注入引擎后调用 CLI ``main``，返回 (退出码, stdout, stderr)。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import numpy as np
import pytest
from nova_core.cli.app import main
from nova_core.engine import Engine
from nova_core.interfaces import EmbeddingProfile

#: 测试用 embedding 维度（小维度让 LanceDB 与断言都轻量）。
TEST_DIM = 16
TEST_PROFILE = EmbeddingProfile(
    model_id="local:test-hash-cli", dim=TEST_DIM, max_input_tokens=512
)


class CountingEmbedding:
    """确定性哈希 embedding（每文本一个单位向量）+ 调用/文本计数。"""

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
# fixture 小仓库内容
# ---------------------------------------------------------------------------

TOKEN_SERVICE_PY = '''"""认证服务：access token 的签发与刷新。"""


class TokenService:
    """access token 的签发与刷新服务。"""

    def refresh_token(self, token: str) -> str:
        """refresh_token 过期后重新签发 access token。"""
        return self._issue(token)

    def _issue(self, token: str) -> str:
        return f"issued:{token}"
'''

LOGGING_PY = '''"""日志工具（与认证无关的旁路模块）。"""


def log_event(name: str) -> str:
    """把事件名格式化成一行日志。"""
    return f"[event] {name}"
'''

AUTH_DESIGN_MD = """# 认证设计

## token 刷新流程

客户端持有的 `refresh_token` 过期后，调用 TokenService.refresh_token 重新签发 access token；
实现见 src/auth/token_service.py。

## 失败处理

签发失败时返回错误码，不做自动重试。
"""

#: fixture 小仓库的初始文件集（路径 → 内容）。
REPO_FILES: dict[str, str] = {
    "src/auth/token_service.py": TOKEN_SERVICE_PY,
    "src/util/logging.py": LOGGING_PY,
    "docs/design/auth.md": AUTH_DESIGN_MD,
}

#: E2E 查询：中文自然语言（≥5 token）+ 两个蛇形/驼峰标识符（Inferred 通道可召回的种子）。
#:
#: 为何两个标识符：``assemble._assess`` 的 ``consensus`` 统计的是"**候选**数"
#: （``len(channel_ranks) >= 2`` 的候选条数 ≥ 2 才 answerable），单个双通道候选不算共识——
#: 这是 Module/03 §4.4 的照抄实现，本卡只按它写断言（已记入执行记录）。
E2E_QUERY = "TokenService 里的 refresh_token 过期以后应该在哪里重新签发"
#: E2E 目标符号（Python 抽取器的 method fqn）。
E2E_TARGET_SYMBOL = "TokenService.refresh_token"
E2E_TARGET_PATH = "src/auth/token_service.py"


def write_repo(root: str | Path, files: dict[str, str]) -> Path:
    """把 ``files`` 写进 ``root``（默认覆盖 REPO_FILES）；返回仓库路径。"""
    base = Path(root)
    for relative, content in files.items():
        target = base / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return base


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def provider() -> CountingEmbedding:
    return CountingEmbedding()


@pytest.fixture
def data_root(tmp_path: Path) -> Path:
    """引擎数据根（每个测试独立，避免 ~/.nova 被污染）。"""
    return tmp_path / "nova-data"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return write_repo(tmp_path / "repo", dict(REPO_FILES))


@pytest.fixture
def engine(data_root: Path, provider: CountingEmbedding) -> Iterator[Engine]:
    """注入 fake provider 的引擎（不触碰真实模型）。"""
    opened = Engine(data_root, provider=provider)
    yield opened
    opened.close()


@pytest.fixture
def run_cli(
    provider: CountingEmbedding, capsys: pytest.CaptureFixture[str]
) -> Callable[..., tuple[int, str, str]]:
    """跑一次 CLI（注入 fake provider），返回 (退出码, stdout, stderr)。"""

    def _run(*argv: str) -> tuple[int, str, str]:
        factory = lambda data: Engine(data, provider=provider)  # noqa: E731 - 测试接缝
        code = main(list(argv), engine_factory=factory)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return _run


@pytest.fixture
def indexed_repo(
    repo: Path, data_root: Path, run_cli: Callable[..., tuple[int, str, str]]
) -> tuple[Path, Path, str]:
    """先把 fixture 仓库索引进独立数据根，返回 (repo, data_root, project_id)。"""
    code, out, err = run_cli("ingest", "--repo", str(repo), "--data", str(data_root))
    assert code == 0, err
    project_id = next(
        line.split(":", 1)[1].strip().split(" ", 1)[0]
        for line in out.splitlines()
        if line.startswith("project:")
    )
    return repo, data_root, project_id
