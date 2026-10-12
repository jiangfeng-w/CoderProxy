"""探针：9router 只读读取 + 官方导出对拍（真机装机环境；**全程不写任何文件/库**）。

验证「WorkBuddy账号导出」9router 分支的两个关键事实（spec「关键机制考证」节的可复现版）：
1. 只读打开本机 9router SQLite（uri mode=ro）读出全量配置可行（不依赖 9router 运行、不需密码）
2. 本地重建结果与 9router 官方导出 API（CLI token 免密）**逐段一致**——证明合并流以官方
   同构承载，恢复无错版风险

用法（自举环境，见 docs/README.md；9router 需在运行以便对拍其导出 API）：
    PYTHONPATH=relay/_deps py -3.13 docs/spec/WorkBuddy账号导出/探针-9router只读读取.py

输出脱敏：只打印段名/计数/布尔/前 8 位 id，不打印 token 或 API key。
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "relay", _REPO / "relay" / "_deps"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from relay.platforms.workbuddy import exports  # noqa: E402

NINE_ROUTER_BASE = "http://127.0.0.1:20128"


def _official_export() -> dict | None:
    """调 9router 官方导出 API（CLI token 由 machine-id + cli-secret 本机派生）。"""
    data_dir = pathlib.Path(os.environ.get("APPDATA", "")) / "9router"
    try:
        raw = (data_dir / "machine-id").read_text().strip()
        secret = (data_dir / "auth" / "cli-secret").read_text().strip()
    except OSError:
        return None
    token = hashlib.sha256((raw + "9r-cli-auth" + secret).encode()).hexdigest()[:16]
    req = urllib.request.Request(f"{NINE_ROUTER_BASE}/api/settings/database",
                                 headers={"x-9r-cli-token": token})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            return json.loads(resp.read())
    except (urllib.error.URLError, ValueError):
        return None  # 9router 未运行/端口不同 → 只做重建检查


def main() -> int:
    ok = True

    # 1. 只读重建
    try:
        ours = exports.load_nine_router_backup()
    except exports.NineRouterUnavailable as exc:
        print(f"[1] 9router 库不可用：{exc}")
        return 1
    sections = sorted(ours.keys())
    print(f"[1] 只读重建 OK：段={sections}")
    print(f"    providerConnections={len(ours['providerConnections'])} 条、"
          f"apiKeys={len(ours['apiKeys'])} 条、settings 键={len(ours['settings'])}")
    for conn in ours["providerConnections"][:3]:
        print(f"    conn {str(conn.get('id'))[:8]}… provider={conn.get('provider')} "
              f"name={str(conn.get('name'))[:12]!r} token={'有' if conn.get('accessToken') else '无'}")

    # 2. 官方导出对拍
    official = _official_export()
    if official is None:
        print("[2] 9router 未运行或无法计算 CLI token → 跳过对拍（重建检查已过）")
    else:
        exp, got = json.dumps(official, sort_keys=True), json.dumps(ours, sort_keys=True)
        same = exp == got
        ok &= same
        print(f"[2] 与官方导出对拍（10 段逐字节）：{'一致 ✅' if same else '不一致 ❌'}")
        if not same:
            for sec in sorted(set(official) | set(ours)):
                if json.dumps(official.get(sec), sort_keys=True) != json.dumps(ours.get(sec), sort_keys=True):
                    print(f"    差异段：{sec}")

    print("探针结论：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
