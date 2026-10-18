import json
def parse(path):
    tools = []; usages = []; answer = ""; ended = False
    for line in open(path, encoding="utf-8"):
        try: e = json.loads(line)
        except ValueError: continue
        t = e.get("type")
        if t == "tool_execution_start": tools.append(e["toolName"])
        elif t == "message_end" and e.get("message", {}).get("role") == "assistant":
            m = e["message"]; usages.append(m.get("usage") or {})
            if m.get("stopReason") == "stop":
                answer = "".join(c.get("text", "") for c in m.get("content", []) if c.get("type") == "text")
        elif t == "agent_end": ended = True
    last = usages[-1] if usages else {}
    return {"tools": tools, "n_tools": len(tools), "turns": len(usages), "ended": ended and bool(answer),
            "ctx": (last.get("input", 0) + last.get("cacheRead", 0) + last.get("cacheWrite", 0) + last.get("output", 0)),
            "in_total": sum(u.get("input", 0) + u.get("cacheRead", 0) + u.get("cacheWrite", 0) for u in usages),
            "out_total": sum(u.get("output", 0) for u in usages),
            "cost": sum((u.get("cost") or {}).get("total", 0) for u in usages), "answer": answer}
