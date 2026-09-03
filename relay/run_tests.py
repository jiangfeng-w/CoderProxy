"""本地跑单测入口（自举依赖，无需全局安装）：`python run_tests.py [-k ...]`。

本机 python 是 shim，不读 PYTHONPATH（见 docs/README.md 自举环境），
故在此显式把项目根与 _deps 加入 sys.path 后再调 pytest。
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

import pytest

if __name__ == "__main__":
    args = ["tests", "-q", *sys.argv[1:]]
    sys.exit(pytest.main(args))
