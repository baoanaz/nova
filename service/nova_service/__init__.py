"""nova-service：服务化外壳（Module/06）。

职责：HTTP API、鉴权（API token / session）、租户映射（token → user → owns project）、
索引 job、审计与可观测。不含检索逻辑（全部来自 nova_core）。
"""

__version__ = "0.0.1"
