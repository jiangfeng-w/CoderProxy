"""relay 启动入口（自举依赖）：`python run.py run` / `python run.py login` ...

本机 python 是 shim，既不读 PYTHONPATH 也不把 cwd 加入 sys.path
（见 docs/README.md 自举环境），故在此显式把项目根与 _deps 加入 sys.path。

标准 Python 环境（已 pip install）仍可用 `python -m relay run`。
"""
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
os.chdir(_ROOT)
for _p in (_ROOT, _ROOT / "_deps"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

from relay.cli import main

if __name__ == "__main__":
    sys.exit(main())
