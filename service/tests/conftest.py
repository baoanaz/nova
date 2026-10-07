"""service 测试公共夹具（TASK-030 建；TASK-031 追加假 provider / EngineManager / 上传助手）。

纪律（`docs/tasks/README.md` 与各卡 DoD）：

- 一律用 ``tmp_path`` 下的临时 data_root，**不依赖本机绝对路径**；
- 不触网、不加载真实 embedding 模型（本文件的确定性假 provider，算法与
  ``core/tests/integration/conftest.py`` 的同名类一致，但**独立实现**：
  ``core/tests`` 不是可导入包，service 测试不得反向依赖 core 的测试代码）；
- 应用实例都走 :func:`nova_service.app.create_app`（生产入口与测试入口同一个）。
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from nova_core.engine import Engine
from nova_core.hashing import blob_hash
from nova_core.interfaces import EmbeddingProfile
from nova_core.text import segment
from nova_core.types import BlobInput, ChangeSet
from nova_service.app import create_app
from nova_service.config import Settings
from nova_service.runtime import EngineManager

#: 仓库根（``service/tests/conftest.py`` → parents[2]）。
REPO_ROOT = Path(__file__).resolve().parents[2]
#: CF-05 合同文件（路径集合的权威来源）。
OPENAPI_CONTRACT = REPO_ROOT / "docs" / "contracts" / "openapi.yaml"

_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})
_PATH_LINE_RE = re.compile(r"^  (/[^:]*):\s*$")
_METHOD_LINE_RE = re.compile(r"^    ([a-z]+):")

#: 假 embedding 维度（小维度让 LanceDB 与断言都轻量）。
FAKE_DIM = 64

#: 端到端小闭环的语料：1 个 python 模块（含目标符号）+ 1 个 markdown 设计文档。
SAMPLE_MODULE_PATH = "src/token_service.py"
SAMPLE_DOC_PATH = "docs/design/token.md"
SAMPLE_MODULE = '''"""令牌服务模块。"""


class TokenService:
    """令牌服务的入口。"""

    def refresh_token(self) -> str:
        """续期令牌：过期后由本方法负责刷新，签发细节见设计文档。"""
        return "old"
'''

SAMPLE_DOC = """# 令牌设计

令牌过期时由 `refresh_token` 刷新。

## 刷新流程

令牌失效后如何续期：refresh_token 重新签发；缓存未命中时回落数据库。
"""

SAMPLE_FILES: dict[str, str] = {
    SAMPLE_MODULE_PATH: SAMPLE_MODULE,
    SAMPLE_DOC_PATH: SAMPLE_DOC,
}
#: 命中目标符号的查询（端到端断言用）。
TARGET_SYMBOL = "TokenService.refresh_token"


# --------------------------------------------------------------------------- 假 provider


def _features(text: str) -> list[str]:
    """文本 → 特征名（jieba 预分词 token 一元/二元 + token 串内字符 bigram）。"""
    tokens = [token for token in segment(text).split() if token]
    features = [f"t1:{token}" for token in tokens]
    features += [f"t2:{left}|{right}" for left, right in itertools.pairwise(tokens)]
    packed = "".join(tokens)
    features += [f"c2:{packed[index:index + 2]}" for index in range(len(packed) - 1)]
    return features


class DeterministicBigramEmbedding:
    """确定性假 ``EmbeddingProvider``（bigram 哈希桶 + L2 归一化；CI 不联网、不加载模型）。"""

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


# --------------------------------------------------------------------------- 应用与引擎


def test_settings(data_root: Path, **overrides: object) -> Settings:
    """测试用配置（临时 data_root；本地模式）。"""
    return Settings(data_root=data_root, local_mode=True, **overrides)  # type: ignore[arg-type]


def make_app(data_root: Path) -> FastAPI:
    """构造应用（测试内需要"全新 app + 额外测试路由"时用）。"""
    return create_app(test_settings(data_root))


def make_invite(app: FastAPI, kind: str = "C", *, max_uses: int = 1) -> str:
    """造一个邀请码并返回码面（TASK-110：注册必须有码）。

    为什么直接写库而不走 ``POST /api/admin/invites``：多数用例关心的只是"有一个真实用户"，
    走后台端点会强制调用方先造出管理员会话，把用例真正要验证的东西埋在准备工作里。
    """
    from nova_service.invites import generate_code
    from nova_service.metadb import MetaDB

    db: MetaDB = app.state.meta_db
    code = generate_code(kind)
    db.create_invite(code, kind, max_uses=max_uses)
    return code


def register_with_invite(
    client: TestClient, app: FastAPI, name: str, password: str, *, kind: str = "C"
) -> dict:
    """经邀请码注册一个用户，返回响应体（断言用；失败时把响应文本一并抛出来）。"""
    response = client.post(
        "/api/auth/register",
        json={"name": name, "password": password, "inviteCode": make_invite(app, kind)},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_client(app: FastAPI) -> TestClient:
    """包一层 TestClient（``raise_server_exceptions=False``：500 信封由应用自己保证）。"""
    return TestClient(app, raise_server_exceptions=False)


def make_manager(data_root: Path) -> EngineManager:
    """构造注入了确定性假 provider 的 ``EngineManager``（不起服务、不触网）。"""
    return EngineManager.open(
        data_root,
        engine_factory=lambda root: Engine.open(root, provider=DeterministicBigramEmbedding()),
    )


def upload_files(
    manager: EngineManager, project_id: str, files: Mapping[str, str | bytes]
) -> dict[str, object]:
    """**服务内部的上传路径**（TASK-033 的 HTTP 面在 TASK-031 尚未存在时用它驱动 E2E）。

    与 TASK-033 ``batch-upload`` 的落盘顺序一致：blob 镜像 → 账本 → 一次 ``ingest``。
    返回 ``{blobHashes, accepted, report}``，便于断言。
    """
    blobs, state = manager.project_paths(project_id)
    accepted: list[str] = []
    added: list[BlobInput] = []
    for path, content in files.items():
        data = content.encode("utf-8") if isinstance(content, str) else content
        digest = blob_hash(path, data)
        blobs.put(path, digest, data)
        state.record_file(path, digest, len(data))
        accepted.append(digest)
        added.append(BlobInput(path=path, content=data, blob_hash=digest))
    state.save()
    report = manager.ingest(project_id, ChangeSet(added=tuple(added)))
    return {"blobHashes": accepted, "accepted": accepted, "report": report}


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """应用的配置对象（data_root 在 tmp_path 下）。"""
    return test_settings(tmp_path / "data")


@pytest.fixture
def engine_manager(settings: Settings) -> Iterator[EngineManager]:
    """注入了假 provider 的引擎管理器（每个测试独立，用后关闭）。"""
    manager = make_manager(settings.data_root)
    try:
        yield manager
    finally:
        manager.close()


@pytest.fixture
def app(settings: Settings, engine_manager: EngineManager) -> FastAPI:
    """临时 data_root + 假 provider 的应用实例（每个测试独立）。"""
    application = create_app(settings)
    application.state.engine_manager = engine_manager  # 预热注入：跳过懒构造
    return application


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    """同步 TestClient（lifespan 打开/关闭）。"""
    with make_client(app) as test_client:
        yield test_client


@pytest.fixture
def project_id(engine_manager: EngineManager) -> str:
    """一个已创建的空项目（用于上传/查询类测试）。"""
    return engine_manager.resolve_project("identity:test-repo", "test-repo").project_id


# --------------------------------------------------------------------------- CF-05 合同


def _parse_contract_paths(text: str) -> dict[str, set[str]]:
    """从 CF-05 的 openapi.yaml 抽出 ``路径 → 方法集合``（不依赖 PyYAML：按缩进解析）。

    只取顶层 ``paths:`` 段下 2 空格缩进的路径键与 4 空格缩进的方法键——这足够表达
    CF-05 的冻结面（路径 + 方法），且不需要引入未声明的第三方依赖。
    """
    paths: dict[str, set[str]] = {}
    current: str | None = None
    in_paths = False
    for line in text.splitlines():
        if line.startswith("paths:"):
            in_paths = True
            continue
        if not in_paths:
            continue
        if line and not line.startswith(" "):  # 顶层键：paths 段结束
            break
        path_match = _PATH_LINE_RE.match(line)
        if path_match:
            current = path_match.group(1)
            paths[current] = set()
            continue
        method_match = _METHOD_LINE_RE.match(line)
        if method_match and current is not None and method_match.group(1) in _METHODS:
            paths[current].add(method_match.group(1))
    return paths


@pytest.fixture(scope="session")
def contract_paths() -> dict[str, set[str]]:
    """CF-05（``docs/contracts/openapi.yaml``）的路径与方法集合。"""
    assert OPENAPI_CONTRACT.is_file(), f"合同文件缺失：{OPENAPI_CONTRACT}"
    parsed = _parse_contract_paths(OPENAPI_CONTRACT.read_text(encoding="utf-8"))
    assert parsed, "合同解析结果为空：openapi.yaml 的 paths 段可能被改写"
    return parsed
