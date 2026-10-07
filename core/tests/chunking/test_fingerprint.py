"""三级配置指纹矩阵（TASK-006-C / D-07）。"""

from __future__ import annotations

from collections.abc import Callable

from nova_core.chunking import (
    EMBEDDING_DIM_KEY,
    EMBEDDING_MODEL_KEY,
    PARSER_CONFIG_KEY,
    IndexFingerprint,
    Invalidation,
    check_fingerprint,
    compute_parser_config_hash,
    stored_fingerprint,
    write_fingerprint,
)
from nova_core.chunking.fingerprint import PARSER_CONFIG_VERSION
from nova_core.interfaces import EmbeddingProfile
from nova_core.storage import Store

PROFILE = EmbeddingProfile(model_id="local:multilingual-e5-small", dim=384, max_input_tokens=512)


def _fingerprint(**overrides: object) -> IndexFingerprint:
    return IndexFingerprint.build(PROFILE, parser_overrides=overrides or None)


def test_fresh_store_needs_no_invalidation(store: Store) -> None:
    assert stored_fingerprint(store) is None
    assert check_fingerprint(store, _fingerprint()) is Invalidation.NONE


def test_written_fingerprint_round_trips(store: Store) -> None:
    current = _fingerprint()
    write_fingerprint(store, current)

    assert stored_fingerprint(store) == current
    assert current.embedding_profile == "local:multilingual-e5-small@384"
    assert check_fingerprint(store, current) is Invalidation.NONE


def test_parser_change_triggers_full_reparse(store: Store) -> None:
    write_fingerprint(store, _fingerprint())

    changed_version = PARSER_CONFIG_VERSION + 1
    assert (
        check_fingerprint(store, _fingerprint(version=changed_version))
        is Invalidation.FULL_REPARSE
    )


def test_embedding_model_change_triggers_reembed(store: Store) -> None:
    write_fingerprint(store, _fingerprint())
    other = IndexFingerprint(
        parser_config_hash=compute_parser_config_hash(),
        embedding_model="api:bge-m3",
        embedding_dim=384,
    )

    assert check_fingerprint(store, other) is Invalidation.REEMBED


def test_embedding_dim_change_triggers_reembed(store: Store) -> None:
    write_fingerprint(store, _fingerprint())
    other = IndexFingerprint(
        parser_config_hash=compute_parser_config_hash(),
        embedding_model=PROFILE.model_id,
        embedding_dim=768,
    )

    assert check_fingerprint(store, other) is Invalidation.REEMBED


def test_parser_change_wins_over_embedding_change(store: Store) -> None:
    write_fingerprint(store, _fingerprint())
    other = IndexFingerprint(
        parser_config_hash=compute_parser_config_hash({"version": 3}),
        embedding_model="api:bge-m3",
        embedding_dim=1024,
    )

    assert check_fingerprint(store, other) is Invalidation.FULL_REPARSE


def test_indexed_files_without_fingerprint_is_conservative(
    store: Store, ingest_file: Callable[..., object]
) -> None:
    """库里有文件但指纹缺失（异常中断）→ 不假设索引可信。"""
    ingest_file("mod.py", "def f():\n    return 1\n")

    assert check_fingerprint(store, _fingerprint()) is Invalidation.FULL_REPARSE


def test_tampered_config_values(store: Store) -> None:
    write_fingerprint(store, _fingerprint())
    store.set_config(PARSER_CONFIG_KEY, "tampered")

    assert check_fingerprint(store, _fingerprint()) is Invalidation.FULL_REPARSE

    write_fingerprint(store, _fingerprint())
    store.set_config(EMBEDDING_MODEL_KEY, "local:other")
    store.set_config(EMBEDDING_DIM_KEY, "512")

    assert stored_fingerprint(store) == IndexFingerprint(
        parser_config_hash=compute_parser_config_hash(),
        embedding_model="local:other",
        embedding_dim=512,
    )
    assert check_fingerprint(store, _fingerprint()) is Invalidation.REEMBED


def test_non_integer_dim_is_treated_as_missing(store: Store) -> None:
    write_fingerprint(store, _fingerprint())
    store.set_config(EMBEDDING_DIM_KEY, "384-ish")

    assert stored_fingerprint(store) is None


def test_parser_config_hash_is_stable_and_covers_registry() -> None:
    first = compute_parser_config_hash()
    assert first == compute_parser_config_hash()
    assert first != compute_parser_config_hash({"extensions": {".py": "python"}})
    assert len(first) == 64
