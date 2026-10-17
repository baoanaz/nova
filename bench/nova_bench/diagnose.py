"""诊断：期望的证据有没有被召回、在哪个通道、最终排第几。

直接在进程内调用引擎的 ``rank_candidates``（与 search 同一条召回与排序路径，只是不做组装与预算
裁剪），所以能看到 top 10 之外的排名——区分「根本没召回」与「召回了但排太低」。
需要 embedding 凭据（查询向量）：从 ``~/.key/nova/secrets.env`` 读取 ``EMBED_*``，不打印。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .targets import Expected, Question

_EMBED_DEFAULTS = {
    "EMBED_MODE": "api",
    "EMBED_MODEL": "voyage-code-4",
    "EMBED_BASE_URL": "https://api.voyageai.com",
    "EMBED_DIM": "1024",
}


def load_embed_env() -> None:
    """把 EMBED_* 放进本进程环境（已有的不覆盖）；缺密钥文件时给出明确报错。"""
    path = Path(os.environ.get("NOVA_SECRETS_FILE", "~/.key/nova/secrets.env")).expanduser()
    if not path.is_file():
        raise SystemExit(f"缺少 {path}（需要 EMBED_API_KEY）")
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(?:export )?(EMBED_[A-Z_]+)=(.*)$", line)
        if match:
            os.environ.setdefault(match.group(1), match.group(2).strip().strip("'\""))
    for key, value in _EMBED_DEFAULTS.items():
        os.environ.setdefault(key, value)


def _matches(candidate, expected: Expected) -> bool:
    if candidate.path != expected.path:
        return False
    return expected.symbol is None or expected.symbol in (candidate.symbol_fqn or "")


def explain(engine, project_id: str, question: Question) -> dict:
    """返回每个期望依据的名次与通道信息。"""
    ranked = engine.rank_candidates(project_id, question.query)
    rows = []
    for expected in question.expected:
        hit = next(((i, c) for i, c in enumerate(ranked, 1) if _matches(c, expected)), None)
        same_path = [
            (i, c.symbol_fqn) for i, c in enumerate(ranked, 1) if c.path == expected.path
        ][:3]
        rows.append(
            {
                "expected": f"{expected.path}::{expected.symbol or '*'}",
                "rank": hit[0] if hit else None,
                "channels": dict(hit[1].channel_ranks) if hit else {},
                "same_path": same_path,
            }
        )
    return {"pool": len(ranked), "rows": rows}
