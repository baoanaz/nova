"""解析 80 次运行（含墙钟耗时）并用 gpt-6-astra 判对错 → judged.jsonl"""
import json, sys, glob, os, re
from concurrent.futures import ThreadPoolExecutor
S = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, "/home/xuwenzheng/github/ace/nova/bench")
from nova_bench import judge
from parse_pi import parse
cfg = judge.load_llm()
qs = {q["id"]: q for q in map(json.loads, open(f"{S}/questions.jsonl"))}
wall = {}
for l in open(f"{S}/run_all.log"):
    m = re.match(r"(\S+) (\S+) exit=(\d+) wall=([\d.]+)", l)
    if m: wall[(m[1], m[2])] = float(m[4])
def one(p):
    run, arm = os.path.basename(p)[:-6].rsplit(".", 1); qid = run.rsplit(".", 1)[0]; q = qs[qid]
    d = parse(p); d.update(run=run, arm=arm, id=qid, repo=q["repo"], wall=wall.get((run, arm)))
    if d["ended"]:
        d["judge"] = judge.chat(cfg, judge.SCENARIO_ASK_PROMPT.format(kind=q["kind"], question=q["query"], reference=q["reference"],
                                output=d["answer"][:10000], gist_rule=judge.GIST_RULE))
    return d
files = [p for p in sorted(glob.glob(f"{S}/runs/*.r[12].*.jsonl"))]
with ThreadPoolExecutor(max_workers=8) as pool, open(f"{S}/judged.jsonl", "w") as out:
    for d in pool.map(one, files):
        out.write(json.dumps(d, ensure_ascii=False) + "\n")
print("judged", len(files))
