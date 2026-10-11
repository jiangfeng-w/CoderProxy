"""WorkBuddy 账号多方式导入：Token/JSON 粘贴、本机客户端登录态、JSON 文件。

本地修订：机制移植自 cockpit-tools（workbuddy_account.rs import_from_json 三级解析 /
workbuddy_oauth.rs build_payload_from_token / commands/workbuddy.rs 本机导入编排，MIT），
与原实现的差异：
- 纯 token 补全加 JWT claims 兜底（原实现 WorkBuddy 链路不解 JWT；本机实测 access token
  为 JWT 且 sub=uid、exp=过期秒——accounts 端点不返回 expires_at，JWT 补齐后 ensure_token
  才能正常判过期）
- 本机导入读官方客户端 auth 文件复用 ide_switch 的密钥派生/解密（原实现走 CodeBuddy CN
  的 vscode secret storage 路径，本项目官方客户端形态是 workbuddy-desktop.info）
- JSON 导入不落 auth_raw/profile_raw/usage_raw（账号池 schema 无这些字段）

导入口径（spec 定稿）：
- 粘贴纯 token：联网 GET /v2/plugin/accounts 补全，失败报错不入库（防坏 token 污染账号池）
- JSON（粘贴或文件）：全程不联网，最简条目只需 access_token
- 本机导入：解密成功即入库（凭证来自官方客户端），配额预取 best-effort

格式兼容（2026-10-12 增补，用户实物文件实测）：
- cockpit-tools export_accounts：数组/单对象、snake 键名——天然兼容
- 9router 设置导出：providerConnections[] 包裹、camelCase、expiresAt 为 ISO 串、
  无 uid（JWT sub 兜底）、name 为显示名；只吃 provider=="codebuddy-cn" 的条目，
  其它平台（codex/kiro 等）跳过
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import time
from datetime import datetime, timezone

from relay.config import settings
from relay.platforms import store
from relay.platforms.base import PlatformUpstreamError
from relay.platforms.workbuddy import ide_switch
from relay.platforms.workbuddy.client import (
    WB_API_PREFIX,
    WbClient,
    parse_envelope,
    plugin_headers,
)
from relay.platforms.workbuddy.oauth import PLATFORM_ID, prefetch_caches

logger = logging.getLogger(__name__)


class ImportParseError(ValueError):
    """导入内容解析失败（路由层映射 400）。"""


# ── 解析层（对齐 cockpit-tools import_from_json 三级降级） ──

_TOKEN_KEYS = ("access_token", "accessToken", "token")
# 选填字段的接受键名（snake/camel 双写，对齐 payload_from_import_value）
_FIELD_KEYS = {
    "uid": ("uid",),
    "nickname": ("nickname",),
    "email": ("email",),
    "enterprise_id": ("enterprise_id", "enterpriseId"),
    "domain": ("domain",),
    "refresh_token": ("refresh_token", "refreshToken"),
}


def normalize_token(raw: str) -> str:
    """`uid+token` 复合串拆分（前段 uid 可弃）；无 + 则整串为 token。"""
    token = (raw or "").strip()
    if "+" in token:
        token = token.split("+", 1)[1].strip()
    return token


def _expiry_ms(value: object) -> int:
    """expires_at 归一：秒/毫秒数字（或数字字符串，<1e11 视为秒）→ 毫秒；
    ISO 8601 字符串（9router expiresAt："2026-11-17T06:42:05.117Z"）→ 毫秒；其余 0。"""
    if isinstance(value, str) and value.strip() and not value.lstrip("+-").isdigit():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    try:
        ts = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    if ts <= 0:
        return 0
    return ts * 1000 if ts < 1e11 else ts


def payload_from_import_value(value: object, index: int | None = None) -> dict:
    """单条导入记录 → payload（缺 access_token 报错；未知键忽略；不联网）。

    键名兼容：cockpit-tools 导出（snake，uid 直接给）+ 9router providerConnections
    条目（camel，无 uid——JWT sub 兜底；name 为显示名兜底）。
    """
    prefix = f"第 {index} 条记录解析失败：" if index is not None else ""
    if not isinstance(value, dict):
        raise ImportParseError(f"{prefix}记录必须是 JSON 对象")
    token = ""
    for key in _TOKEN_KEYS:
        if value.get(key):
            token = str(value[key]).strip()
            break
    if not token:
        raise ImportParseError(f"{prefix}缺少 access_token")
    token = normalize_token(token) or token
    payload: dict = {"access_token": token, "source": "json"}
    for field, keys in _FIELD_KEYS.items():
        for key in keys:
            if value.get(key):
                payload[field] = str(value[key]).strip()
                break
    if not payload.get("nickname") and value.get("name"):
        payload["nickname"] = str(value["name"]).strip()  # 9router 显示名
    if not payload.get("uid"):
        sub = decode_jwt_claims(token).get("sub")  # 9router 无 uid → JWT sub 兜底
        if sub:
            payload["uid"] = str(sub).strip()
    for key in ("expires_at", "expiresAt"):
        if value.get(key) is not None:
            payload["expires_at"] = _expiry_ms(value[key])
            break
    if not payload.get("expires_at") and value.get("expiresIn") is not None:
        # 9router expiresIn = 秒偏移（expiresAt 缺失时的回退口径）
        try:
            payload["expires_at"] = int(time.time() * 1000) + int(value["expiresIn"]) * 1000
        except (TypeError, ValueError):
            pass
    return payload


def parse_import_text(text: str) -> list[dict]:
    """粘贴/文件文本 → payload 列表：`{`/`[` 开头走 JSON，否则整串当纯 token。

    纯 token 只产 `{access_token, source:"token"}`——联网补全在 import_text 编排层做
    （解析层保持纯函数可测）。
    """
    stripped = (text or "").strip()
    if not stripped:
        raise ImportParseError("请输入 Token 或 JSON")
    if stripped[0] in "{[":
        return _parse_import_json(stripped)
    token = normalize_token(stripped)
    if not token:
        raise ImportParseError("未解析到 access token")
    return [{"access_token": token, "source": "token"}]


def _parse_import_json(text: str) -> list[dict]:
    try:
        doc = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ImportParseError(f"不是有效 JSON：{exc}") from exc
    if isinstance(doc, list):
        if not doc:
            raise ImportParseError("导入 JSON 数组为空")
        return [payload_from_import_value(item, i + 1) for i, item in enumerate(doc)]
    if isinstance(doc, dict):
        if any(doc.get(key) for key in _TOKEN_KEYS):
            return [payload_from_import_value(doc)]
        for key in ("accounts", "items"):
            items = doc.get(key)
            if isinstance(items, list):
                if not items:
                    raise ImportParseError(f"导入 JSON 的 {key} 数组为空")
                return [payload_from_import_value(item, i + 1) for i, item in enumerate(items)]
        # 9router 设置导出：providerConnections[]（只吃 codebuddy-cn，其它平台跳过）
        conns = doc.get("providerConnections")
        if isinstance(conns, list):
            picked, skipped = [], 0
            for conn in conns:
                if not isinstance(conn, dict) or not any(conn.get(k) for k in _TOKEN_KEYS):
                    continue
                if conn.get("provider") not in (None, "codebuddy-cn"):
                    skipped += 1
                    continue
                picked.append(conn)
            if not picked:
                hint = f"（{skipped} 条其它平台账号已跳过）" if skipped else ""
                raise ImportParseError(f"providerConnections 中无 codebuddy-cn 账号{hint}")
            return [payload_from_import_value(item, i + 1) for i, item in enumerate(picked)]
        # 无包裹也无 token：按单条记录解析，报出「缺少 access_token」（cockpit 三级降级口径）
        return [payload_from_import_value(doc)]
    raise ImportParseError("导入 JSON 必须是账号对象或数组（支持 accounts/items 包裹）")


def decode_jwt_claims(token: str) -> dict:
    """JWT payload 解码（base64url，不验签——仅身份预填/过期兜底）；失败返回 {}。"""
    parts = (token or "").split(".")
    if len(parts) != 3:
        return {}
    padded = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(padded))
    except (ValueError, json.JSONDecodeError):
        return {}
    return claims if isinstance(claims, dict) else {}


# ── 纯 token 联网补全（对齐 build_payload_from_token；失败报错不入库） ──


async def complete_from_accounts_api(access_token: str) -> dict:
    """GET /v2/plugin/accounts（本机实测契约：code=0 + data.accounts[]，lastLogin 标记）。

    JWT claims 兜底（accounts 端点不返回 expires_at/email；实测 sub=uid、exp=过期秒）。
    """
    claims = decode_jwt_claims(access_token)
    client = WbClient()
    try:
        body = await client.request_json(
            "GET", f"{settings.wb_api_base}{WB_API_PREFIX}/accounts",
            headers=plugin_headers(access_token=access_token))
    finally:
        await client.close()
    info = parse_envelope(body, context="plugin/accounts")
    accounts = info.get("accounts")
    if not isinstance(accounts, list) or not accounts:
        raise PlatformUpstreamError("plugin/accounts 响应缺少 accounts 数组")
    item = next((a for a in accounts if isinstance(a, dict) and a.get("lastLogin")), accounts[0])
    if not isinstance(item, dict):
        raise PlatformUpstreamError("plugin/accounts 响应格式异常")
    exp = claims.get("exp")
    return {
        "access_token": access_token,
        "refresh_token": "",
        "uid": str(item.get("uid") or claims.get("sub") or ""),
        "nickname": str(item.get("nickname") or claims.get("nickname") or ""),
        "email": "",
        "enterprise_id": str(item.get("enterpriseId") or claims.get("enterprise_id") or ""),
        "domain": "",
        "expires_at": int(exp) * 1000 if isinstance(exp, (int, float)) and exp > 0 else 0,
        "source": "token",
    }


# ── 本机导入（读官方客户端登录态，复用 ide_switch 密钥派生/解密） ──


async def _local_payload() -> dict:
    auth_file = ide_switch.auth_file_path()
    marker = ide_switch.logout_marker_path(auth_file) if auth_file else None
    if auth_file is None or not auth_file.exists() or (marker is not None and marker.exists()):
        raise ImportParseError("未在本机 WorkBuddy 客户端中找到登录信息")
    try:
        raw = await asyncio.to_thread(auth_file.read_text, encoding="utf-8")
        doc = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ImportParseError(f"本机登录信息不是有效 JSON：{exc}") from exc
    key = await ide_switch.get_official_key()
    decoded = ide_switch.decode_session(doc, key)
    auth = decoded.get("auth") if isinstance(decoded, dict) else None
    account = decoded.get("account") if isinstance(decoded, dict) else None
    token = str((auth or {}).get("accessToken") or "").strip()
    if not token:
        raise ImportParseError("本机登录信息中未找到 access token")
    expires_at = (auth or {}).get("expiresAt")
    return {
        "access_token": normalize_token(token) or token,
        "refresh_token": str((auth or {}).get("refreshToken") or ""),
        "uid": str((account or {}).get("uid") or ""),
        "nickname": str((account or {}).get("nickname") or ""),
        "email": "",
        "enterprise_id": str((account or {}).get("enterpriseId") or ""),
        "domain": str((auth or {}).get("domain") or ""),
        "expires_at": _expiry_ms(expires_at) if expires_at else 0,
        "source": "local",
    }


# ── 编排入库（upsert preserve：同 uid 覆盖凭证保留 checkin/quota 缓存） ──


def _to_entry(payload: dict) -> dict:
    token = str(payload.get("access_token") or "").strip()
    uid = str(payload.get("uid") or "").strip()
    return {
        "uid": uid or f"wb_{hashlib.sha256(token.encode()).hexdigest()[:12]}",
        "nickname": str(payload.get("nickname") or ""),
        "email": str(payload.get("email") or ""),
        "enterprise_id": str(payload.get("enterprise_id") or ""),
        "domain": str(payload.get("domain") or ""),
        "access_token": token,
        "refresh_token": str(payload.get("refresh_token") or ""),
        "expires_at": int(payload.get("expires_at") or 0),
        "status": "normal",
    }


async def import_text(text: str) -> list[dict]:
    """粘贴/JSON 文件统一入口；返回入库后的账号行（含 token，路由层经 AccountView 出口）。"""
    payloads = parse_import_text(text)
    imported: list[dict] = []
    for payload in payloads:
        if payload.get("source") == "token":
            payload = await complete_from_accounts_api(payload["access_token"])
        entry = _to_entry(payload)
        account = await store.upsert_account(PLATFORM_ID, entry, preserve=True)
        if payload.get("source") == "token":
            await prefetch_caches(account)  # best-effort；JSON 源不联网（对齐 cockpit）
        imported.append(account)
        logger.info("[workbuddy] 导入账号 uid=%s source=%s", account.get("uid"),
                    payload.get("source"))
    return imported


async def import_local() -> dict:
    """读本机官方客户端当前登录账号入库（解密成功即入库，配额预取 best-effort）。"""
    payload = await _local_payload()
    entry = _to_entry(payload)
    account = await store.upsert_account(PLATFORM_ID, entry, preserve=True)
    await prefetch_caches(account)
    logger.info("[workbuddy] 本机导入账号 uid=%s", account.get("uid"))
    return account
