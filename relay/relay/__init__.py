"""CoderProxy relay 包。

⚠️ 副作用：本模块被 import 时立即禁用系统代理（NO_PROXY=*），
使所有 httpx 请求直连（等价 trust_env=False）。这是 M1 探针得出的硬性结论——
本机 httpx 默认读系统代理会把 lc.yinhaiyun.com 走隧道导致连不上。
保留 TRUST_ENV_PROXY=true 环境变量以在调试时恢复系统代理行为。
"""
from __future__ import annotations

import os as _os

if not _os.environ.get("TRUST_ENV_PROXY", "").lower() in ("1", "true", "yes"):
    _os.environ["NO_PROXY"] = "*"
    _os.environ["no_proxy"] = "*"

__version__ = "0.1.0"
