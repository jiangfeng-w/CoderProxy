"""WorkBuddy 账号导出：cockpit-tools 兼容格式 + 9router 完整备份合并流。

两种导出目标（spec「WorkBuddy账号导出」定稿，2026-10-12）：

1) cockpit 兼容格式：`workbuddy_accounts_*.json` 形态纯数组——可直接导入
   cockpit-tools（其 import_from_json 逐条 upsert）或本应用回导（同 uid 覆盖）。
   id 规则 = `workbuddy_{md5(uid)}`（实物反推验证）；时间戳单位：expires_at 毫秒、
   created_at/last_used/usage_updated_at 秒。

2) 9router 完整备份（读取合并流）：**只读**打开本机 9router SQLite
   （%APPDATA%/9router/db/data.sqlite，uri mode=ro，绝不写），按 0.5.99 `exportDb`
   同构重建全量导出，再把选中账号合并进 providerConnections（用户拍板①：只增/只更新
   选中的，其余连接与其它配置段原样保留），输出**完整可恢复备份**由用户在 9router
   手动恢复（9router 导入是整库覆盖语义，瘦文件会清空其配置——故必须全量承载）。
   读不到库 → 降级剪贴板条目片段（用户手动合并）。

匹配与更新策略：
- 按 accessToken 的 JWT `sub`（=uid）匹配已有 codebuddy-cn 连接；账号侧 uid 与 token
  sub 都作为候选键（token 轮换后 sub 不变）
- 命中的连接：更新凭证组（accessToken/refreshToken/expiresAt/expiresIn）+ updatedAt，
  并**清除绑定旧凭证的瞬态错误态**（testStatus→active、errorCode/backoffLevel/
  lastError*/rateLimitedUntil/modelLock_* 复位——旧错误态保留会让恢复后的号被
  9router 可用性判定误伤）；连接本体（id/name/email/priority/isActive/createdAt）不动
- 未命中的连接：一切字段原样（拍板①）
- 选中但无匹配：append 新连接（新 UUID、provider=codebuddy-cn、authType=oauth、
  priority=全池最大+1、createdAt/updatedAt=now）

本地修订：重建口径对齐 9router 0.5.99 `db/index.js exportDb`（读实测导出对拍）；
apiKeys 的 access 字段做版本自适应（0.5.99 有 accessRestricted/accessAllow 列 →
`access:{restricted,allow}`；旧版无列则不输出）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from relay.platforms import store
from relay.platforms.workbuddy.imports import decode_jwt_claims

logger = logging.getLogger(__name__)

PLATFORM_ID = "workbuddy"
NINE_ROUTER_PROVIDER = "codebuddy-cn"

# 9router 导出的十个段（exportDb 同构；usage/requestDetails 不在其导出口径内）
_SECTION_TABLES = ("providerConnections", "providerNodes", "proxyPools", "combos")


class NineRouterUnavailable(RuntimeError):
    """读不到 9router 库（未安装/路径异常）→ 调用方降级剪贴板片段。"""


class ExportRequestError(ValueError):
    """导出请求非法（未知格式/无可导出账号）→ 路由层 400。"""


class ExportAccountNotFound(LookupError):
    """指定 uid 不在账号池 → 路由层 404。"""


# ── cockpit 兼容格式 ──


def build_cockpit_export(accounts: list[dict], now_s: int | None = None) -> list[dict]:
    """账号池行 → cockpit-tools 导出形态（纯数组；缺失字段不输出）。

    字段口径对齐用户实物（`workbuddy_accounts_2026-10-11.json`，19 字段）中账号池
    可承载的部分：id/email/uid/nickname/access_token/refresh_token/token_type/
    expires_at/domain/status/created_at/last_used/usage_updated_at（+enterprise_id
    非空时带上）。quota_raw/auth_raw 等快照段账号池没有，不输出（cockpit 导入容忍）。
    """
    ts = int(time.time()) if now_s is None else now_s
    out: list[dict] = []
    for a in accounts:
        uid = str(a.get("uid") or "")
        item = {
            "id": f"workbuddy_{hashlib.md5(uid.encode()).hexdigest()}",
            # cockpit 的 email 字段在其实物里是昵称兜底值（其本机导入的降级口径）
            "email": str(a.get("email") or a.get("nickname") or uid),
            "uid": uid,
            "nickname": str(a.get("nickname") or ""),
            "access_token": str(a.get("access_token") or ""),
            "refresh_token": str(a.get("refresh_token") or ""),
            "token_type": "Bearer",
            "expires_at": int(a.get("expires_at") or 0),
            "domain": str(a.get("domain") or ""),
            "status": str(a.get("status") or "normal"),
            "created_at": ts,
            "last_used": ts,
            "usage_updated_at": ts,
        }
        if a.get("enterprise_id"):
            item["enterprise_id"] = str(a["enterprise_id"])
        out.append(item)
    return out


# ── 9router：只读重建导出 ──


def nine_router_db_path() -> Path | None:
    """9router 数据库路径：env RELAY_9ROUTER_DB 覆盖 → %APPDATA%/9router/db/data.sqlite。"""
    override = os.environ.get("RELAY_9ROUTER_DB", "").strip()
    if override:
        return Path(override)
    appdata = os.environ.get("APPDATA", "").strip()
    if not appdata:
        return None
    return Path(appdata) / "9router" / "db" / "data.sqlite"


def _export_rows(con: sqlite3.Connection, table: str,
                 json_columns: tuple[str, ...] = ()) -> list[dict]:
    """表行 → 导出对象：data JSON 展开 + 其余列覆盖（isActive 归一 bool）。"""
    out: list[dict] = []
    # table 常量来自模块内固定元组（非外部输入），无注入面
    for row in con.execute(f"SELECT * FROM {table}"):
        keys = row.keys()
        item: dict = json.loads(row["data"] or "{}") if "data" in keys else {}
        for col in keys:
            if col == "data":
                continue
            value = row[col]
            if col in json_columns and isinstance(value, str):
                try:
                    value = json.loads(value) if value else []
                except ValueError:
                    value = []
            if col == "isActive":
                value = bool(value)
            item[col] = value
        out.append(item)
    return out


def _read_api_keys(con: sqlite3.Connection) -> list[dict]:
    """apiKeys 段（版本自适应：0.5.99 起 access 两列 → 嵌套 access 对象）。"""
    cols = {c[1] for c in con.execute("PRAGMA table_info(apiKeys)")}
    out: list[dict] = []
    for row in con.execute("SELECT * FROM apiKeys"):
        item = {
            "id": row["id"],
            "key": row["key"],
            "name": row["name"],
            "machineId": row["machineId"],
            "isActive": bool(row["isActive"]),
            "createdAt": row["createdAt"],
        }
        if "accessRestricted" in cols or "accessAllow" in cols:
            allow_raw = row["accessAllow"] if "accessAllow" in cols else None
            try:
                allow = json.loads(allow_raw) if allow_raw else []
            except ValueError:
                allow = []
            item["access"] = {
                "restricted": bool(row["accessRestricted"]) if "accessRestricted" in cols else False,
                "allow": allow,
            }
        out.append(item)
    return out


def _read_kv_scopes(con: sqlite3.Connection) -> dict:
    """kv 四个 scope → modelAliases{}/customModels[]/mitmAlias{}/pricing{}。"""
    aliases: dict = {}
    custom: list = []
    mitm: dict = {}
    pricing: dict = {}
    rows = con.execute(
        "SELECT scope, key, value FROM kv "
        "WHERE scope IN ('modelAliases','customModels','mitmAlias','pricing')")
    for scope, key, value in rows:
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            continue
        if scope == "modelAliases":
            aliases[key] = parsed
        elif scope == "customModels":
            custom.append(parsed)
        elif scope == "mitmAlias":
            mitm[key] = parsed
        elif scope == "pricing":
            pricing[key] = parsed
    return {"modelAliases": aliases, "customModels": custom, "mitmAlias": mitm, "pricing": pricing}


def load_nine_router_backup() -> dict:
    """只读重建 9router 全量导出（与 0.5.99 exportDb 同构；全程不写库）。"""
    path = nine_router_db_path()
    if path is None or not path.is_file():
        raise NineRouterUnavailable("未检测到本机 9router 数据库（%APPDATA%/9router/db/data.sqlite）")
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise NineRouterUnavailable(f"9router 数据库打开失败：{exc}") from exc
    try:
        con.row_factory = sqlite3.Row
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "providerConnections" not in tables:
            raise NineRouterUnavailable("9router 数据库结构异常（缺 providerConnections 表）")
        row = con.execute("SELECT data FROM settings WHERE id = 1").fetchone()
        backup: dict = {"settings": json.loads(row["data"]) if row else {}}
        for table in _SECTION_TABLES:
            backup[table] = (
                _export_rows(con, table, json_columns=("models",))
                if table == "combos" else _export_rows(con, table))
        backup["apiKeys"] = _read_api_keys(con)
        backup.update(_read_kv_scopes(con))
        return backup
    except sqlite3.Error as exc:
        raise NineRouterUnavailable(f"9router 数据库读取失败：{exc}") from exc
    finally:
        con.close()


# ── 9router：合并与片段 ──


def _iso_from_ms(ms: int) -> str:
    """毫秒时间戳 → 9router ISO 串（`2026-11-17T06:42:05.117Z`）。"""
    base = datetime.fromtimestamp(ms // 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    return f"{base}.{ms % 1000:03d}Z"


def _iso_now() -> str:
    return _iso_from_ms(int(time.time() * 1000))


def _token_sub(token: str) -> str:
    sub = decode_jwt_claims(token).get("sub")
    return str(sub).strip() if sub else ""


def _account_keys(account: dict) -> set[str]:
    """账号匹配候选键：池内 uid + token 的 JWT sub（轮换后 sub 不变）。"""
    keys = {str(account.get("uid") or "").strip(), _token_sub(str(account.get("access_token") or ""))}
    return {k for k in keys if k}


def _credential_fields(account: dict) -> dict:
    exp_ms = int(account.get("expires_at") or 0)
    fields = {
        "accessToken": str(account.get("access_token") or ""),
        "refreshToken": str(account.get("refresh_token") or ""),
    }
    if exp_ms > 0:
        fields["expiresAt"] = _iso_from_ms(exp_ms)
        fields["expiresIn"] = max(0, (exp_ms - int(time.time() * 1000)) // 1000)
    else:
        fields["expiresAt"] = None
        fields["expiresIn"] = 0
    return fields


def _new_connection(account: dict, priority: int) -> dict:
    """新连接条目（对齐用户实物 9router 备份的 codebuddy-cn 条目形态）。"""
    conn = {
        **_credential_fields(account),
        "testStatus": "active",
        "errorCode": None,
        "backoffLevel": 0,
        "id": str(uuid.uuid4()),
        "provider": NINE_ROUTER_PROVIDER,
        "authType": "oauth",
        "name": str(account.get("nickname") or account.get("uid") or "WorkBuddy"),
        "email": str(account.get("email") or "") or None,
        "priority": priority,
        "isActive": True,
        "createdAt": _iso_now(),
        "updatedAt": _iso_now(),
    }
    return conn


def merge_selected(backup: dict, accounts: list[dict]) -> dict:
    """把选中账号合并进备份的 providerConnections（拍板①：其余连接与配置段原样）。"""
    merged = json.loads(json.dumps(backup))  # 深拷贝（JSON 安全），未触碰项保持不变
    conns = merged.get("providerConnections")
    if not isinstance(conns, list):
        conns = []
        merged["providerConnections"] = conns
    by_sub: dict[str, int] = {}
    max_priority = 0
    for idx, conn in enumerate(conns):
        if not isinstance(conn, dict):
            continue
        if isinstance(conn.get("priority"), int):
            max_priority = max(max_priority, conn["priority"])
        if conn.get("provider") == NINE_ROUTER_PROVIDER:
            sub = _token_sub(str(conn.get("accessToken") or ""))
            if sub:
                by_sub[sub] = idx
    next_priority = max_priority + 1
    for account in accounts:
        keys = _account_keys(account)
        idx = next((by_sub[k] for k in keys if k in by_sub), None)
        if idx is not None:
            conn = conns[idx]
            assert isinstance(conn, dict)
            conn.update(_credential_fields(account))
            conn["updatedAt"] = _iso_now()
            # 清瞬态错误态：绑定旧凭证的错误判定对新凭证失效（防被可用性判定误伤）
            conn.update({"testStatus": "active", "errorCode": None, "backoffLevel": 0,
                         "lastError": None, "lastErrorAt": None, "rateLimitedUntil": None})
            for key in list(conn):
                if key.startswith("modelLock_"):
                    conn[key] = None
        else:
            conn = _new_connection(account, next_priority)
            conns.append(conn)
            next_priority += 1
            for k in _account_keys(account):
                by_sub[k] = len(conns) - 1
    return merged


def build_nine_router_fragment(accounts: list[dict]) -> list[dict]:
    """降级片段：仅选中账号的候选连接条目（用户手动合并进备份数组）。"""
    return [_new_connection(a, idx + 1) for idx, a in enumerate(accounts)]


# ── 编排（路由层入口） ──


def _export_filename(fmt: str, mode: str) -> str:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if fmt == "cockpit":
        return f"workbuddy_accounts_{day}.json"
    if mode == "full_backup":
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
        return f"9router-backup-merged-{stamp}.json"
    return f"workbuddy-9router-fragment-{day}.json"


async def export_selected(uids: list[str], fmt: str) -> dict:
    """路由层入口：返回 {format, mode, filename, content, note}。"""
    if fmt not in ("cockpit", "9router"):
        raise ExportRequestError(f"不支持的导出格式: {fmt}")
    accounts = await store.load_accounts(PLATFORM_ID)
    if uids:
        wanted = {str(u).strip() for u in uids if str(u).strip()}
        selected = [a for a in accounts if a.get("uid") in wanted]
        missing = wanted - {a.get("uid") for a in selected}
        if missing:
            raise ExportAccountNotFound(f"账号不存在: {', '.join(sorted(missing))}")
    else:
        selected = accounts
    if not selected:
        raise ExportRequestError("没有可导出的账号")
    if fmt == "cockpit":
        payload = build_cockpit_export(selected)
        return {
            "format": "cockpit",
            "mode": "cockpit",
            "filename": _export_filename("cockpit", "cockpit"),
            "content": json.dumps(payload, ensure_ascii=False, indent=2),
            "note": "cockpit-tools 兼容格式：可直接在 cockpit-tools 导入（同 uid 覆盖），"
                    "或用本应用「Token / JSON」导入回导（用于备份/迁移）。",
        }
    try:
        backup = await asyncio.to_thread(load_nine_router_backup)
    except NineRouterUnavailable as exc:
        fragment = build_nine_router_fragment(selected)
        return {
            "format": "9router",
            "mode": "fragment",
            "filename": _export_filename("9router", "fragment"),
            "content": json.dumps(fragment, ensure_ascii=False, indent=2),
            "note": f"{exc}。已按「条目片段」导出：请把数组内容合并进你的 9router 备份文件的 "
                    "providerConnections 数组（9router 导入是整库覆盖语义，瘦文件会清空其配置），"
                    "再在 9router 设置里恢复。",
        }
    merged = merge_selected(backup, selected)
    return {
        "format": "9router",
        "mode": "full_backup",
        "filename": _export_filename("9router", "full_backup"),
        "content": json.dumps(merged, ensure_ascii=False, indent=2),
        "note": "已读取本机 9router 配置并合并选中账号（其余连接与配置原样保留）："
                "完整备份文件，直接在 9router 设置里恢复即可。注意 9router 恢复是整库覆盖语义。",
    }
