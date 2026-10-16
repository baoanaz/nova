#!/usr/bin/env python3
"""benchmark 入口：见 nova_bench/cli.py 与 bench/README.md。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from nova_bench.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
