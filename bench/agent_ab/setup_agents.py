"""生成两套隔离的 Pi 配置目录：agent-nova（带本地 bench 的 nova MCP）与 agent-base（无 MCP）。

key 从环境变量 XIUGOU_API_KEY 读取，只写进本目录下的 models.json（已被 .gitignore 忽略），测完请删除。
"""
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
key = os.environ["XIUGOU_API_KEY"]
for arm in ("nova", "base"):
    d = os.path.join(here, f"agent-{arm}"); os.makedirs(d, mode=0o700, exist_ok=True)
    models = {"providers": {"xiugou": {"baseUrl": "https://xiugou.de5.net/v1", "api": "openai-responses", "apiKey": key,
        "compat": {"sendSessionAffinityHeaders": True},
        "models": [{"id": "gpt-6.1-sol", "name": "GPT-6.1 Sol", "reasoning": True, "input": ["text"], "contextWindow": 272000,
                    "maxTokens": 32000, "cost": {"input": 4, "output": 20, "cacheRead": 0.4, "cacheWrite": 5}}]}}}
    path = os.path.join(d, "models.json")
    with open(path, "w") as f: json.dump(models, f, indent=1)
    os.chmod(path, 0o600)
    json.dump({"defaultProvider": "xiugou", "defaultModel": "gpt-6.1-sol", "quietStartup": True}, open(os.path.join(d, "settings.json"), "w"))
    mcp = {"mcpServers": {"nova": {"url": sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8894/mcp",
                                   "exposure": "direct", "timeout": 60}}} if arm == "nova" else {"mcpServers": {}}
    json.dump(mcp, open(os.path.join(d, "mcp.json"), "w"))
print("ok")
