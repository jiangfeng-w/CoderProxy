"""探针：WorkBuddy IDE 切号只读校验（真机装机环境；**全程不写任何文件**）。

验证链路（spec「本机实测证据」节的可复现版）：
1. 官方 exe（ELECTRON_RUN_AS_NODE=1）派生加密密钥 → 校验 44 字节输出
2. 读本机 workbuddy-desktop.info → 检查 keyId 与派生密钥一致
3. 解密白名单字段（auth.accessToken/refreshToken、账号 PII）→ 明文非空
4. round-trip：decode → encode（随机 nonce）→ decode → 与首次 decode 结果一致

用法（自举环境，见 docs/README.md）：
    PYTHONPATH=relay/_deps py -3.13 docs/spec/WorkBuddy-IDE账号切换/探针-IDE切换只读校验.py

输出脱敏：只打印长度/keyId/布尔，不打印任何 token 或 PII 明文。
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

from relay.platforms.workbuddy import ide_switch as sw  # noqa: E402


def main() -> int:
    ok = True

    # 1. 密钥派生
    key = asyncio.run(sw.get_official_key())
    print(f"[1] 密钥派生 OK：keyId={key.id}（32B，sha256 前 16 hex）")

    # 2. 本机登录态文件
    auth_file = sw.auth_file_path()
    assert auth_file is not None and auth_file.exists(), f"登录态文件不存在：{auth_file}"
    doc = json.loads(auth_file.read_text(encoding="utf-8"))
    assert sw.contains_encrypted_wrapper(doc), "本机文件不含 $wbEncrypted 包装（口径变化？）"
    envelope = json.loads(__import__("base64").b64decode(doc["auth"]["accessToken"]["envelope"]))
    match = envelope.get("keyId") == key.id
    ok &= match
    print(f"[2] 文件 keyId={envelope.get('keyId')} 与派生密钥一致：{match}")

    # 3. 解密白名单字段
    decoded = sw.decode_session(doc, key)
    token = decoded.get("auth", {}).get("accessToken", "")
    refresh = decoded.get("auth", {}).get("refreshToken", "")
    nickname = decoded.get("account", {}).get("nickname", "")
    have_token = bool(token)
    ok &= have_token
    print(f"[3] 解密 OK：accessToken {len(token)} 字符（JWT={token[:4] + '…' if have_token else '空'}）、"
          f"refreshToken {len(refresh)} 字符、nickname {len(nickname)} 字符（不回显）")

    # 4. round-trip：encode（随机 nonce）→ decode → 与首次 decode 全等
    encoded = sw.encode_session(decoded, key)
    assert sw.contains_encrypted_wrapper(encoded), "encode 后应含包装"
    again = sw.decode_session(encoded, key)
    same = again == decoded
    ok &= same
    nonce1 = json.loads(__import__("base64").b64decode(encoded["auth"]["accessToken"]["envelope"]))["nonce"]
    nonce0 = envelope["nonce"]
    print(f"[4] round-trip 全等：{same}（随机 nonce 生效：{nonce1 != nonce0}）")

    print("探针结论：", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
