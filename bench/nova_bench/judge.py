"""LLM 审核：自动出题 → 调用真实工具 → LLM 按评分表打分并指出问题。

和固定题库互补：固定题库回答“有没有退步”，这里回答“换一批没见过的题，工具返回的内容
对 Agent 到底有没有用”。题目由 LLM 根据索引里随机抽到的函数生成（自带参考依据），
评分也由 LLM 给出，所以只当作发现问题的线索，不当作绝对分数；每条都保留原文便于人工复核。

LLM 用 ``~/.key/nova/secrets.env`` 里的 ``ANSWER_*``（与 ask 同一个模型），密钥不会被打印。
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path

import httpx

SECRETS = Path(os.environ.get("NOVA_SECRETS_FILE", "~/.key/nova/secrets.env")).expanduser()
_SKIP_PATH = re.compile(
    r"(^|/)(tests?|__tests__|examples?|docs?|docs_src|samples?|demo|scripts|\.agents|vendor|third_party)(/|$)"
    r"|(^|/)test_|_test\.|\.test\.|\.spec\.",
    re.IGNORECASE,
)

GEN_PROMPT = """你在为“代码检索工具”出评测题。下面是某个开源仓库里的一段代码。
请写一个真实开发者在**不知道这段代码存在**时会问的问题，答案应当能由这段代码回答。
要求：
- 不要出现函数名、类名、文件名等原文标识符（开发者事先并不知道它们）；
- 用{lang}提问；问题要具体，不要泛泛而谈；
- 同时给出 2~3 个“好答案必须包含的事实”。
只输出 JSON：{{"question": "...", "facts": ["...", "..."]}}

文件：{path}
代码：
{code}
"""

SEARCH_JUDGE_PROMPT = """你是严格的评审，评估“代码检索工具”返回给 AI 编程助手的内容。
问题：{question}
参考依据（这是已知的标准答案所在位置）：{path} :: {symbol}
```
{code}
```
工具返回的内容（Markdown，可能被截断到前 {limit} 字符）：
-----
{output}
-----
请只输出 JSON：
{{"found": true/false  // 返回内容里是否包含参考依据（或实质等价的代码）
 ,"best_rank": 整数或null  // 最相关证据是第几条
 ,"relevance": 1-5  // 返回的证据整体与问题相关的程度
 ,"sufficiency": 1-5  // 仅凭返回内容，助手能否回答问题
 ,"noise": 1-5  // 5=几乎没有无关内容，1=大量无关内容或元信息
 ,"issue": "最主要的一个问题，没有就写 无"
 ,"fix": "如果要改进检索/输出，最值得做的一件事"}}
"""

ASK_JUDGE_PROMPT = """你是严格的评审，评估“代码问答工具”的回答。
问题：{question}
参考依据（标准答案所在位置）：{path} :: {symbol}
```
{code}
```
好答案必须包含的事实：{facts}
工具的回答：
-----
{output}
-----
请只输出 JSON：
{{"correct": 1-5  // 事实是否正确、覆盖了必要事实
 ,"grounded": 1-5  // 结论是否都有引用支撑、没有编造
 ,"useful": 1-5  // 对开发者是否清楚、够用、不啰嗦
 ,"hallucination": true/false
 ,"issue": "最主要的一个问题，没有就写 无"
 ,"fix": "如果要改进，最值得做的一件事"}}
"""


def load_llm() -> dict[str, str]:
    cfg: dict[str, str] = {}
    for line in SECRETS.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(?:export )?(ANSWER_[A-Z_]+)=(.*)$", line.strip())
        if match:
            cfg[match.group(1)] = match.group(2).strip().strip("'\"")
    missing = [k for k in ("ANSWER_BASE_URL", "ANSWER_API_KEY", "ANSWER_MODEL") if not cfg.get(k)]
    if missing:
        raise SystemExit(f"缺少 {SECRETS} 里的 {missing}")
    return cfg


def chat(cfg: dict[str, str], prompt: str, *, max_tokens: int = 4000) -> dict:
    """调用 OpenAI 兼容接口并解析第一个 JSON 对象；失败返回 ``{"_error": ...}``。"""
    url = cfg["ANSWER_BASE_URL"].rstrip("/") + "/chat/completions"
    body = {
        "model": cfg["ANSWER_MODEL"],
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {cfg['ANSWER_API_KEY']}"}
    last = "empty"
    for attempt in range(4):
        try:
            response = httpx.post(url, json=body, headers=headers, timeout=120)
            if response.status_code >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            text = response.json()["choices"][0]["message"]["content"] or ""
            if not text.strip():  # 推理模型的思考吃光了额度：加倍重试
                body["max_tokens"] = min(body["max_tokens"] * 2, 16000)
                continue
            match = re.search(r"\{.*\}", text, re.DOTALL)
            return json.loads(match.group(0)) if match else {"_error": "no json", "_text": text[:200]}
        except Exception as exc:  # noqa: BLE001 - 评审工具容错，记录原因即可
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
            time.sleep(2)
    return {"_error": last}


def sample_chunks(conn, n: int, seed: int) -> list[dict]:
    """从索引里抽 n 个“像样的”函数/方法/类 chunk（非测试/示例/文档，15~120 行）。"""
    rows = conn.execute(
        "SELECT file_path, symbol_fqn, symbol_kind, start_line, end_line, content FROM chunks "
        "WHERE symbol_kind IN ('function','method','class') AND end_line - start_line BETWEEN 14 AND 120"
    ).fetchall()
    pool = [dict(r) for r in rows if not _SKIP_PATH.search(r["file_path"] or "")]
    random.Random(seed).shuffle(pool)
    seen_files: set[str] = set()
    picked: list[dict] = []
    for row in pool:
        if row["file_path"] in seen_files:
            continue
        seen_files.add(row["file_path"])
        picked.append(row)
        if len(picked) >= n:
            break
    return picked


def strip_repo_prefix(path: str) -> str:
    """索引里的路径带仓库名前缀（``fastapi/routing.py``）；给 LLM 看的原样即可。"""
    return path


def render_report(cases: list[dict], meta: dict) -> str:
    lines = ["# LLM 审核报告", "", f"- 模型：{meta['model']}　题数：{len(cases)}　种子：{meta['seed']}", ""]
    for tool in ("search", "ask"):
        rows = [c for c in cases if c["tool"] == tool and "judge" in c and "_error" not in c["judge"]]
        if not rows:
            continue

        def avg(key: str, rows: list[dict] = rows) -> float:
            vals = [c["judge"][key] for c in rows if isinstance(c["judge"].get(key), (int, float))]
            return sum(vals) / len(vals) if vals else 0.0

        if tool == "search":
            found = sum(1 for c in rows if c["judge"].get("found"))
            lines += [f"## search（{len(rows)} 题）", "",
                      f"- 返回内容包含参考依据：{found}/{len(rows)}",
                      f"- 相关度 {avg('relevance'):.2f} · 够不够答 {avg('sufficiency'):.2f} · 噪声(5=干净) {avg('noise'):.2f}",
                      f"- 平均返回 {sum(c['tokens'] for c in rows)/len(rows):.0f} token", ""]
        else:
            lines += [f"## ask（{len(rows)} 题）", "",
                      f"- 正确 {avg('correct'):.2f} · 有依据 {avg('grounded'):.2f} · 有用 {avg('useful'):.2f}",
                      f"- 幻觉：{sum(1 for c in rows if c['judge'].get('hallucination'))}/{len(rows)}", ""]
        lines += ["| 仓库 | 题目 | 评分 | 主要问题 | 建议 |", "|---|---|---|---|---|"]
        for c in rows:
            j = c["judge"]
            score = (f"found={j.get('found')} rel={j.get('relevance')} suf={j.get('sufficiency')} noise={j.get('noise')}"
                     if tool == "search" else f"c={j.get('correct')} g={j.get('grounded')} u={j.get('useful')}")
            q = c["question"].replace("|", "/")[:60]
            lines.append(f"| {c['repo']} | {q} | {score} | {str(j.get('issue'))[:90]} | {str(j.get('fix'))[:90]} |")
        lines.append("")
    errors = [c for c in cases if "_error" in c.get("judge", {}) or "error" in c]
    if errors:
        lines += [f"## 失败 {len(errors)} 条", ""] + [f"- {c['repo']}: {c.get('error') or c['judge']}" for c in errors]
    return "\n".join(lines) + "\n"
