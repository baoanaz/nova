"""TASK-033 验收：``/api/sync/*``（batch-upload / checkpoint / deletions / status）。"""

from __future__ import annotations

import base64
import time

import pytest
from fastapi.testclient import TestClient
from nova_core.hashing import blob_hash
from nova_core.storage import Store
from nova_service.routers.sync import MAX_BATCH_BYTES, checkpoint_id_for
from nova_service.runtime import EngineManager
from nova_service.sync_state import MAX_CHECKPOINTS

from tests.conftest import SAMPLE_FILES, SAMPLE_MODULE_PATH, TARGET_SYMBOL

#: 删除端到端用的两个文件：各自带一个独有符号，便于断言"删干净"。
ALPHA_PATH = "src/alpha.py"
ALPHA_SOURCE = '"""alpha 模块。"""\n\n\ndef alpha_only_symbol() -> str:\n    return "alpha"\n'
BETA_PATH = "src/beta.py"
BETA_SOURCE = '"""beta 模块。"""\n\n\ndef beta_only_symbol() -> str:\n    return "beta"\n'
ALPHA_SYMBOL = "alpha_only_symbol"


def encode_blob(path: str, content: bytes | str) -> dict[str, str]:
    """按 CF-05 编码一个 blob（``contentB64`` = base64(原始字节) + CF-02 的 ``blobHash``）。"""
    data = content.encode("utf-8") if isinstance(content, str) else content
    return {
        "path": path,
        "blobHash": blob_hash(path, data),
        "contentB64": base64.b64encode(data).decode("ascii"),
    }


def upload(
    client: TestClient, project_id: str, files: dict[str, str | bytes], **extra: object
) -> dict:
    blobs = [encode_blob(path, content) for path, content in files.items()]
    response = client.post(
        "/api/sync/batch-upload", json={"projectId": project_id, "blobs": blobs, **extra}
    )
    assert response.status_code == 200, response.text
    return response.json()


def store_counts(engine_manager: EngineManager, project_id: str) -> dict[str, int]:
    with Store.open(engine_manager.project_dir(project_id)) as store:
        return store.counts()


@pytest.fixture
def project(client: TestClient) -> str:
    """一个空项目（HTTP 建，走的就是 client 的路径）。"""
    return client.post(
        "/api/projects/resolve", json={"identityKey": "identity:sync-repo", "displayName": "sync"}
    ).json()["projectId"]


# --------------------------------------------------------------------------- batch-upload


def test_upload_is_idempotent(
    client: TestClient, engine_manager: EngineManager, project: str
) -> None:
    """同一批上传两次：两次 ``accepted`` 相同，且 chunk 数不翻倍。"""
    first = upload(client, project, SAMPLE_FILES)
    chunks_after_first = store_counts(engine_manager, project)["chunks"]
    second = upload(client, project, SAMPLE_FILES)

    assert first["accepted"] == second["accepted"]
    assert len(second["accepted"]) == len(SAMPLE_FILES)
    assert second["report"]["chunksNew"] == 0, "第二次是纯复用（同内容同 id）"
    assert second["report"]["chunksReused"] > 0
    assert store_counts(engine_manager, project)["chunks"] == chunks_after_first
    assert first["report"]["added"] == len(SAMPLE_FILES), "空账本起步：都算 added"
    assert second["report"]["modified"] == len(SAMPLE_FILES), "账本已有：第二遍算 modified"
    assert first["skipped"] == []


def test_upload_reports_skipped_binary_files(
    client: TestClient, engine_manager: EngineManager, project: str
) -> None:
    """二进制文件如实进 ``skipped``（D-30 诚实性：索引没成的文件必须能被客户端看见）。"""
    payload = upload(
        client,
        project,
        {
            "src/blob.bin": b"\x00\x01\x02binary\x00",
            SAMPLE_MODULE_PATH: SAMPLE_FILES[SAMPLE_MODULE_PATH],
        },
    )
    assert payload["skipped"] == ["src/blob.bin"]
    assert payload["report"]["skippedFiles"] == ["src/blob.bin"]
    assert payload["report"]["added"] == 1, "只有可解析的文件计入 added"


def test_upload_rejects_tampered_hash_without_touching_state(
    client: TestClient, engine_manager: EngineManager, project: str
) -> None:
    """``blobHash`` 与 CF-02 不一致 → 400，且账本与索引**未被修改**。"""
    item = encode_blob(SAMPLE_MODULE_PATH, SAMPLE_FILES[SAMPLE_MODULE_PATH])
    item["blobHash"] = "f" * 64
    response = client.post(
        "/api/sync/batch-upload", json={"projectId": project, "blobs": [item]}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "blob_hash_mismatch"

    assert store_counts(engine_manager, project)["chunks"] == 0
    state = engine_manager.sync_state(project)
    assert state.files == {}
    assert engine_manager.blob_store(project).usage() == (0, 0)


def test_upload_batch_size_limit(
    client: TestClient, project: str
) -> None:
    """解码后总字节 > 1MiB → 413 ``batch_too_large``。"""
    huge = b"x" * (MAX_BATCH_BYTES + 1)
    response = client.post(
        "/api/sync/batch-upload",
        json={"projectId": project, "blobs": [encode_blob("src/huge.py", huge)]},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "batch_too_large"


@pytest.mark.parametrize("path", ["../evil.py", "/etc/passwd", "a\\b.py"])
def test_upload_rejects_unsafe_paths(client: TestClient, project: str, path: str) -> None:
    response = client.post(
        "/api/sync/batch-upload",
        json={"projectId": project, "blobs": [encode_blob(path, "x = 1\n")]},
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_path"


def test_upload_rejects_bad_base64_and_empty_batch(client: TestClient, project: str) -> None:
    bad = encode_blob("src/a.py", "x = 1\n")
    bad["contentB64"] = "!!!not-base64!!!"
    response = client.post(
        "/api/sync/batch-upload", json={"projectId": project, "blobs": [bad]}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_content_encoding"

    empty = client.post("/api/sync/batch-upload", json={"projectId": project, "blobs": []})
    assert empty.status_code == 400
    assert empty.json()["error"]["code"] == "empty_batch"


def test_upload_unknown_project_is_404(client: TestClient) -> None:
    response = client.post(
        "/api/sync/batch-upload",
        json={
            "projectId": "0123456789abcdef",
            "blobs": [encode_blob("src/a.py", "x = 1\n")],
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"


def test_upload_records_branch_and_commit(client: TestClient, project: str) -> None:
    upload(client, project, {SAMPLE_MODULE_PATH: SAMPLE_FILES[SAMPLE_MODULE_PATH]},
           branch="main", commit="deadbeef")
    status = client.get(f"/api/sync/status/{project}").json()
    assert status["branch"] == "main"
    assert status["commit"] == "deadbeef"


# --------------------------------------------------------------------------- closed loop


def test_upload_then_search_finds_the_new_file(client: TestClient, project: str) -> None:
    """与 TASK-032 的联动：上传后立即查询能检索到新上传的文件（上传→索引→检索闭环）。"""
    upload(client, project, SAMPLE_FILES)
    response = client.post(
        "/api/query/search", json={"projectId": project, "query": TARGET_SYMBOL}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["meta"]["evidenceCount"] >= 1
    assert SAMPLE_MODULE_PATH in body["markdown"]


# --------------------------------------------------------------------------- deletions


def test_delete_is_end_to_end_and_idempotent(client: TestClient, project: str) -> None:
    """上传 A（独有符号）与 B → 命中 A → 删 A → 不再返回 A；``unknown`` 幂等；blobs 下降。"""
    upload(client, project, {ALPHA_PATH: ALPHA_SOURCE, BETA_PATH: BETA_SOURCE})

    before = client.post(
        "/api/query/search", json={"projectId": project, "query": ALPHA_SYMBOL}
    ).json()
    assert ALPHA_PATH in before["markdown"]
    status_before = client.get(f"/api/sync/status/{project}").json()
    assert status_before["blobs"]["count"] == 2

    deleted = client.post(
        "/api/sync/deletions", json={"projectId": project, "paths": [ALPHA_PATH]}
    )
    assert deleted.status_code == 200
    assert deleted.json() == {"deleted": [ALPHA_PATH], "unknown": []}

    after = client.post(
        "/api/query/search", json={"projectId": project, "query": ALPHA_SYMBOL}
    ).json()
    assert ALPHA_PATH not in after["markdown"], "删除后不得再返回该文件的证据"
    status_after = client.get(f"/api/sync/status/{project}").json()
    assert status_after["blobs"]["count"] == 1
    assert status_after["filesIndexed"] == 1

    again = client.post(
        "/api/sync/deletions", json={"projectId": project, "paths": [ALPHA_PATH]}
    )
    assert again.status_code == 200
    assert again.json() == {"deleted": [], "unknown": [ALPHA_PATH]}, "重复删除 → unknown，不报错"


def test_delete_removes_only_the_deleted_paths_blob(client: TestClient, project: str) -> None:
    """CF-02：``blob_hash`` 含 path，因此“同内容不同 path”是两个独立 blob，删一个只少一个。"""
    shared = "value = 1\n"
    upload(client, project, {"src/one.py": shared, "src/two.py": shared})
    assert client.get(f"/api/sync/status/{project}").json()["blobs"]["count"] == 2

    client.post("/api/sync/deletions", json={"projectId": project, "paths": ["src/one.py"]})
    status = client.get(f"/api/sync/status/{project}").json()
    assert status["blobs"]["count"] == 1
    assert status["filesIndexed"] == 1


def test_delete_keeps_blob_still_referenced_by_another_path(
    client: TestClient, engine_manager: EngineManager, project: str
) -> None:
    """引用计数分支：账本里两个 path 共享同一 hash 时，删一个不删镜像（删完才删）。

    （真实上传不会出现共享：``blob_hash`` 含 path。该分支服务于将来的差分协议/历史账本，
    因此用直接写账本的方式确定性覆盖。）
    """
    digest = "a" * 64
    blobs, state = engine_manager.project_paths(project)
    blobs.put("src/one.py", digest, b"value = 1\n")
    state.record_file("src/one.py", digest, 10)
    state.record_file("src/two.py", digest, 10)
    state.save()

    client.post("/api/sync/deletions", json={"projectId": project, "paths": ["src/one.py"]})
    assert blobs.usage()[0] == 1, "另一 path 仍引用该 hash：镜像必须保留"

    client.post("/api/sync/deletions", json={"projectId": project, "paths": ["src/two.py"]})
    assert blobs.usage()[0] == 0, "无人引用才删镜像"


# --------------------------------------------------------------------------- checkpoints


def test_checkpoint_is_content_addressed_and_lru_capped(client: TestClient, project: str) -> None:
    hashes = [f"{index:064x}" for index in range(3)]
    first = client.post(
        "/api/sync/checkpoint", json={"projectId": project, "blobHashes": hashes}
    ).json()["checkpointId"]
    # 同一集合（乱序 + 重复）必须得到同一 id；不同集合必须不同。
    repeat = client.post(
        "/api/sync/checkpoint",
        json={"projectId": project, "blobHashes": [*reversed(hashes), hashes[0]]},
    ).json()["checkpointId"]
    other = client.post(
        "/api/sync/checkpoint", json={"projectId": project, "blobHashes": hashes[:2]}
    ).json()["checkpointId"]
    assert first == repeat == checkpoint_id_for(hashes)
    assert other != first

    for index in range(3, 6):
        client.post(
            "/api/sync/checkpoint",
            json={"projectId": project, "blobHashes": [f"{index:064x}"]},
        )
    assert (
        client.get(f"/api/sync/status/{project}").json()["checkpoints"] == MAX_CHECKPOINTS
    ), "第 4 个之后最早的被淘汰（账本里 ≤3）"


def test_checkpoint_empty_set_is_accepted(client: TestClient, project: str) -> None:
    response = client.post("/api/sync/checkpoint", json={"projectId": project, "blobHashes": []})
    assert response.status_code == 200
    assert response.json()["checkpointId"] == checkpoint_id_for([])


# --------------------------------------------------------------------------- status


def test_status_matches_store_counts_and_blob_bytes(
    client: TestClient, engine_manager: EngineManager, project: str
) -> None:
    upload(client, project, {**SAMPLE_FILES, ALPHA_PATH: ALPHA_SOURCE})
    status = client.get(f"/api/sync/status/{project}").json()
    counts = store_counts(engine_manager, project)

    assert status["projectId"] == project
    assert status["filesIndexed"] == counts["files"]
    assert status["chunks"] == counts["chunks"]
    assert status["symbols"] == counts["symbols"]
    assert status["edges"] == counts["edges"]
    assert status["pendingJobs"] == 0
    assert status["indexingFiles"] == [], "同步索引：请求内完成，不伪造进行中的文件"
    assert status["lastIndexedAt"] is not None

    expected_bytes = sum(
        len(content.encode("utf-8")) for content in [*SAMPLE_FILES.values(), ALPHA_SOURCE]
    )
    assert status["blobs"] == {"count": 3, "bytes": expected_bytes}
    actual_bytes = sum(
        item.stat().st_size
        for item in engine_manager.blob_store(project).root.rglob("*")
        if item.is_file()
    )
    assert status["blobs"]["bytes"] == actual_bytes


def test_status_unknown_project_is_404(client: TestClient) -> None:
    response = client.get("/api/sync/status/0123456789abcdef")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "project_not_found"


# --------------------------------------------------------------------------- 性能观测


def test_single_batch_upload_latency_is_measured(
    client: TestClient, project: str
) -> None:
    """实测单批耗时（TASK-062 的输入；不设硬门禁，只记录到执行记录）。"""
    files = {
        f"src/mod_{index}.py": f'"""m{index}"""\n\n\ndef f_{index}() -> int:\n    return {index}\n'
        for index in range(20)
    }
    started = time.perf_counter()
    payload = upload(client, project, files)
    elapsed = time.perf_counter() - started
    assert payload["report"]["added"] == 20
    assert elapsed < 60, f"单批 20 文件耗时异常：{elapsed:.2f}s"
    print(f"\n[perf] 单批 20 文件 / {sum(len(c) for c in files.values())} 字节 → {elapsed:.3f}s")
