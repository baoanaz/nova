"""LLM 审核：自动出题 → 调用真实工具 → LLM 按评分表打分并指出问题。

和固定题库互补：固定题库回答“有没有退步”，这里回答“换一批没见过的题，工具返回的内容
对 Agent 到底有没有用”。题目由 LLM 根据索引里随机抽到的函数生成（自带参考依据），
评分也由 LLM 给出，所以只当作发现问题的线索，不当作绝对分数；每条都保留原文便于人工复核。

LLM 默认用 ``~/.key/nova/secrets.env`` 里的 ``ANSWER_*``（与 ask 同一个模型）；配了 ``JUDGE_*``
就用它评审，避免“自己评自己”。密钥不会被打印。
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

GIST_RULE = """先给二值结论 gist：站在“助手只拿到这段内容”的角度，它能否答对问题的大意——
核心机制、关键位置/调用关系正确，足以回答用户或直接开始改代码，而不必另起一轮搜索。
缺少次要细节（具体常量值、边角分支、某个变量名）不影响 gist=true；核心机制缺失、答错或会误导才判 false。
参考答案只用来核对事实，不要求逐条覆盖。"""

SCENARIO_SEARCH_PROMPT = """你是严格的评审。一个 AI 编程助手在真实开发场景里向“代码检索工具”提了问题，下面是工具返回的上下文。
场景类型：{kind}
问题：{question}
人工读代码后写下的参考答案（要点与所在位置）：
{reference}
工具返回的内容（Markdown，可能被截断到前 {limit} 字符）：
-----
{output}
-----
{gist_rule}
只输出 JSON：
{{"gist": true/false
 ,"gist_reason": "一句话说明 gist 判定依据"
 ,"coverage": 1-5  // 参考答案里的关键要点/位置，返回内容覆盖了多少（5=全部）
 ,"missing": ["返回里缺少的关键要点或文件，没有则空数组"]
 ,"actionable": 1-5  // 仅凭返回，助手能否直接回答或动手（5=无需再搜）
 ,"noise": 1-5  // 5=几乎全是有用内容，1=大量无关内容或元信息
 ,"wasted": ["最没价值的返回片段（简述，最多2条）"]
 ,"issue": "最主要的一个问题，没有写 无"
 ,"fix": "最值得做的一件改进（针对检索/排序/裁剪/提示信息）"}}
"""

SCENARIO_ASK_PROMPT = """你是严格的评审。开发者在真实场景里向“代码问答工具”提问，下面是它的回答。
场景类型：{kind}
问题：{question}
人工读代码后写下的参考答案：
{reference}
工具的回答：
-----
{output}
-----
{gist_rule}
只输出 JSON：
{{"gist": true/false
 ,"gist_reason": "一句话说明 gist 判定依据"
 ,"correct": 1-5  // 与参考答案相比，关键要点是否正确、完整
 ,"grounded": 1-5  // 结论是否有引用支撑、没有编造
 ,"useful": 1-5  // 对开发者是否直接可用、不啰嗦、结构清晰
 ,"hallucination": true/false
 ,"missing": ["回答漏掉的关键要点，没有则空数组"]
 ,"wrong": ["回答里错误或无依据的陈述，没有则空数组"]
 ,"issue": "最主要的一个问题，没有写 无"
 ,"fix": "最值得做的一件改进"}}
"""


def load_llm() -> dict[str, str]:
    """评审模型配置：``JUDGE_*`` 优先（与 ask 不同的模型），没配则回落到 ``ANSWER_*``。"""
    cfg: dict[str, str] = {}
    for line in SECRETS.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^(?:export )?((?:ANSWER|JUDGE)_[A-Z_]+)=(.*)$", line.strip())
        if match:
            cfg[match.group(1)] = match.group(2).strip().strip("'\"")
    for key in ("BASE_URL", "API_KEY", "MODEL"):
        cfg[f"ANSWER_{key}"] = os.environ.get(f"JUDGE_{key}") or cfg.get(f"JUDGE_{key}") or cfg.get(f"ANSWER_{key}", "")
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


def load_scenarios(root: Path, repos: list[str] | None) -> list[dict]:
    """读取 ``bench/scenarios/<repo>.jsonl``（人工读过仓库后写的场景题）。"""
    out: list[dict] = []
    for f in sorted(root.glob("*.jsonl")):
        if repos and f.stem not in repos:
            continue
        for line in f.read_text(encoding="utf-8").splitlines():
            if line.strip():
                item = json.loads(line)
                item["repo"] = f.stem
                out.append(item)
    return out


def render_scenario_report(cases: list[dict], meta: dict) -> str:
    lines = ["# 场景评测报告", "", f"- 评审模型：{meta['model']}　题数：{len(cases)}", "",
             "## 大意通过率（gist：仅凭返回能否答对大意/直接动手）", "",
             "| 集合 | search | ask |", "|---|---|---|"]
    for split in ("tune", "holdout", None):
        cells = []
        for tool in ("search", "ask"):
            rows = [c for c in cases if c["tool"] == tool and "gist" in c.get("judge", {})
                    and (split is None or c.get("split", "tune") == split)]
            ok = sum(1 for c in rows if c["judge"]["gist"] is True)
            cells.append(f"{ok}/{len(rows)}" + (f"（{ok / len(rows):.0%}）" if rows else ""))
        lines.append(f"| {split or '全部'} | {cells[0]} | {cells[1]} |")
    lines.append("")
    for tool in ("search", "ask"):
        rows = [c for c in cases if c["tool"] == tool and "judge" in c and "_error" not in c["judge"]]
        if not rows:
            continue

        def avg(key: str, rows: list[dict] = rows) -> float:
            vals = [c["judge"][key] for c in rows if isinstance(c["judge"].get(key), (int, float))]
            return sum(vals) / len(vals) if vals else 0.0

        if tool == "search":
            hit = sum(c["path_hits"][0] for c in rows)
            tot = sum(c["path_hits"][1] for c in rows)
            lines += [f"## search（{len(rows)} 题）", "",
                      f"- 参考文件命中 {hit}/{tot} · 要点覆盖 {avg('coverage'):.2f} · 可直接行动 {avg('actionable'):.2f} · "
                      f"噪声(5=干净) {avg('noise'):.2f}",
                      f"- 平均返回 {sum(c['tokens'] for c in rows)/len(rows):.0f} token · {sum(c['ms'] for c in rows)/len(rows):.0f} ms", ""]
        else:
            lines += [f"## ask（{len(rows)} 题）", "",
                      f"- 正确 {avg('correct'):.2f} · 有依据 {avg('grounded'):.2f} · 有用 {avg('useful'):.2f} · "
                      f"幻觉 {sum(1 for c in rows if c['judge'].get('hallucination'))}/{len(rows)}",
                      f"- 平均 {sum(c['ms'] for c in rows)/len(rows):.0f} ms", ""]
        for c in rows:
            j = c["judge"]
            if tool == "search":
                score = f"文件{c['path_hits'][0]}/{c['path_hits'][1]} cov={j.get('coverage')} act={j.get('actionable')} noise={j.get('noise')} {c['tokens']}tok"
            else:
                score = f"c={j.get('correct')} g={j.get('grounded')} u={j.get('useful')} halluc={j.get('hallucination')}"
            lines += [f"### {c['id']} · {c['kind']} · gist={j.get('gist')} · {score}", f"- 问：{c['question']}",
                      f"- 大意判定：{j.get('gist_reason')}",
                      f"- 缺：{j.get('missing')}", f"- 问题：{j.get('issue')}", f"- 建议：{j.get('fix')}"]
            if tool == "search":
                lines.append(f"- 低价值片段：{j.get('wasted')}")
            else:
                lines.append(f"- 错误陈述：{j.get('wrong')}")
            lines.append("")
    errors = [c for c in cases if "_error" in c.get("judge", {}) or "error" in c]
    if errors:
        lines += [f"## 失败 {len(errors)} 条", ""] + [f"- {c['id']}: {c.get('error') or c['judge']}" for c in errors]
    return "\n".join(lines) + "\n"


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


AGENT_PROMPT = """你是在开发者本地仓库里工作的 AI 编程助手（类似 Claude Code）。当前仓库：{repo}（主要语言 {lang}）。
你看不到代码，只能通过下面两个 MCP 工具了解仓库。用户刚发来一条请求。

工具 1：search_context
描述：{search_desc}
参数 query：{search_param}

工具 2：ask_project
描述：{ask_desc}
参数 question：{ask_param}

用户请求：
{request}

请严格按两个工具描述里的指引，分别写出：如果用 search_context 解决这条请求，你会（并行）发出的 1~3 条 query
（像真实助手一样，按需要决定条数）；如果用 ask_project，你会传的 question；再说明你会优先用哪一个。只输出 JSON：
{{"search_queries": ["..."], "ask_question": "...", "preferred": "search_context 或 ask_project", "why": "一句话"}}"""


def load_tool_descriptions(root: Path) -> dict[str, dict]:
    """Agent 实际看到的 MCP 工具描述（``docs/contracts/mcp-tools.json``，与 client 副本一致）。"""
    payload = json.loads((root / "docs" / "contracts" / "mcp-tools.json").read_text(encoding="utf-8"))
    out = {}
    for tool in payload["tools"]:
        schema = tool.get("inputSchema") or tool.get("input_schema") or {}
        props = schema.get("properties", {})
        out[tool["name"]] = {"description": tool["description"],
                             "params": {k: v.get("description", "") for k, v in props.items()}}
    return out
