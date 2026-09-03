"""conftest：把项目根与本地依赖目录加入 sys.path（docs 自举环境）。"""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for _p in (_ROOT, _ROOT / "_deps"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)
