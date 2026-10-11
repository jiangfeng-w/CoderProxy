"""探针：/v2/plugin/accounts 端点契约只读校验（真机装机环境；**不写任何文件、不入库**）。

验证「WorkBuddy账号多方式导入」的核心依赖契约（spec「本机实测证据」节的可复现版）：
1. 从本机官方客户端登录态解密出 access token（复用 IDE 切号的密钥派生/解密，全程内存）
2. GET /v2/plugin/accounts（Bearer + plugin 域头族）→ code=0 + data.accounts[]
3. 响应字段形态：lastLogin 项存在、uid/nickname 非空；确认不含 email/refreshToken/expiresAt
   （这决定导入侧的 JWT 兜底设计）

用法（自举环境，见 docs/README.md）：
    PYTHONPATH=relay/_deps py -3.13 docs/spec/WorkBuddy账号多方式导入/探针-账号端点契约.py

输出脱敏：只打印字段名/布尔/长度，不打印任何 token 或 PII 明文。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "relay", _REPO / "relay" / "_deps"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from relay.config import settings  # noqa: E402
from relay.platforms.workbuddy import ide_switch as sw  # noqa: E402
from relay.platforms.workbuddy.client import (  # noqa: E402
    WB_API_PREFIX,
    WbClient,
    plugin_headers,
)


def main() -> int:
    ok = True

    # 1. 本机登录态解密（只读，token 只留内存）
    auth_file = sw.auth_file_path()
    assert auth_file is not None and auth_file.exists(), f"登录态文件不存在：{auth_file}"
    key = asyncio.run(sw.get_official_key())
    doc = json.loads(auth_file.read_text(encoding="utf-8"))
    decoded = sw.decode_session(doc, key)
    token = str(decoded["auth"]["accessToken"])
    print(f"[1] 本机登录态解密 OK：accessToken {len(token)} 字符（不回显）")

    # 2. GET /v2/plugin/accounts
    async def probe():
        client = WbClient()
        try:
            return await client.request_json(
                "GET", f"{settings.wb_api_base}{WB_API_PREFIX}/accounts",
                headers=plugin_headers(access_token=token))
        finally:
            await client.close()

    body = asyncio.run(probe())
    code = body.get("code")
    accounts = (body.get("data") or {}).get("accounts")
    match = code == 0 and isinstance(accounts, list) and len(accounts) > 0
    ok &= match
    print(f"[2] /v2/plugin/accounts：code={code}，accounts 数量="
          f"{len(accounts) if isinstance(accounts, list) else 0}（预期 code=0 且非空）")

    # 3. 字段形态（字段名级脱敏）
    item = next((a for a in accounts if a.get("lastLogin")), accounts[0])
    fields = sorted(item.keys())
    has_core = bool(item.get("uid")) and bool(item.get("nickname"))
    has_last_login = any(a.get("lastLogin") for a in accounts)
    lacks = [k for k in ("email", "refreshToken", "expiresAt") if k in fields]
    ok &= has_core and has_last_login
    print(f"[3] lastLogin 项：uid/nickname 非空={has_core}；lastLogin 标记存在={has_last_login}")
    print(f"    字段清单：{fields}")
    print(f"    不含 email/refreshToken/expiresAt（JWT 兜底设计依据）：{not lacks}"
          + (f"（实际含 {lacks}，需复核 spec）" if lacks else ""))

    print("探针结论：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
