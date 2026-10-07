"""nova-core CLI（TASK-013 §B）。

``core/pyproject.toml`` 的 console script 指向本模块的 :func:`main`：

```toml
[project.scripts]
nova-core = "nova_core.cli:main"
```

子模块分工：

- ``app``：argparse 参数面与四个子命令（ingest / search / status / eval）；
- ``eval``：golden runner（recall@5 / recall@10 / MRR + Markdown 报告），
  同时被 ``benches/run.py`` 复用（同一份实现，不复制）。
"""

from nova_core.cli.app import build_parser, main

__all__ = ["build_parser", "main"]
