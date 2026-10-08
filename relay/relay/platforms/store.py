"""平台多账号 JSON 存储：platform_{platform_id}.json，与 relay_state.json 完全隔离。

结构（占位符示例，不含真实凭证）：
{"version": 1, "accounts": [{"uid", "nickname", "email", "enterprise_id", "domain",
  "access_token", "refresh_token", "expires_at", "status",
  "checkin": {"last_checkin_date", "streak_days", "today_checked_in"},
  "quota": {"total", "remaining", "used", "expire_at", "fetched_at"}}]}

- 落盘纪律照抄 relay/storage.py（双锁 + asyncio.to_thread + tmp.replace 原子写），
  但按 platform_id 持**独立锁实例**，多平台并存互不阻塞。
- upsert 按 uid 去重；重登录覆盖 token 但**保留旧 checkin/quota 缓存**（preserve）。
- expires_at 统一毫秒（秒/毫秒双口径归一：0 < v < 1e11 视为秒 ×1000）。
- today_checked_in 读取时按 Asia/Shanghai（固定 UTC+8，无夏令时）本地日期失效：
  last_checkin_date != 今天 → 视为未签到（内存修正，不落盘）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from relay.config import settings

logger = logging.getLogger(__name__)

# Asia/Shanghai 固定 UTC+8（无夏令时，等价且零 tzdata 依赖）
CN_TZ = timezone(timedelta(hours=8))

# 锁注册表：按 platform_id 惰性创建独立锁实例（元锁只保护注册表本身）
_locks: dict[str, tuple[asyncio.Lock, threading.Lock]] = {}
_locks_guard = threading.Lock()


def _get_locks(platform_id: str) -> tuple[asyncio.Lock, threading.Lock]:
    with _locks_guard:
        pair = _locks.get(platform_id)
        if pair is None:
            pair = (asyncio.Lock(), threading.Lock())
            _locks[platform_id] = pair
        return pair


def today_cn() -> str:
    """Asia/Shanghai 本地日期（YYYY-MM-DD）——签到「今天」的唯一判定口径。"""
    return datetime.now(CN_TZ).strftime("%Y-%m-%d")


def normalize_expires_ms(value) -> int:
    """expires_at 归一为毫秒时间戳：秒口径（<1e11）×1000；非法值归 0。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return 0
    if v <= 0:
        return 0
    return v * 1000 if v < 100_000_000_000 else v


def _path(platform_id: str) -> Path:
    return Path(settings.data_dir) / f"platform_{platform_id}.json"


def _load_sync(platform_id: str) -> list[dict]:
    with _get_locks(platform_id)[1]:
        p = _path(platform_id)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            logger.warning("[platforms/%s] 存储文件损坏，按空账号处理: %s", platform_id, p)
            return []
    accounts = data.get("accounts") if isinstance(data, dict) else None
    return [a for a in accounts if isinstance(a, dict)] if isinstance(accounts, list) else []


def _save_sync(platform_id: str, accounts: list[dict]) -> None:
    with _get_locks(platform_id)[1]:
        p = _path(platform_id)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"version": 1, "accounts": accounts}, ensure_ascii=False, indent=2),
            "utf-8")
        tmp.replace(p)


def _daily_invalidate(account: dict) -> dict:
    """读取时按本地日期失效 today_checked_in（不落盘：展示层永远看正确态）。"""
    checkin = account.get("checkin")
    if (isinstance(checkin, dict) and checkin.get("today_checked_in")
            and checkin.get("last_checkin_date") != today_cn()):
        account["checkin"] = {**checkin, "today_checked_in": False}
    return account


async def load_accounts(platform_id: str) -> list[dict]:
    accounts = await asyncio.to_thread(_load_sync, platform_id)
    return [_daily_invalidate(dict(a)) for a in accounts]


async def find_account(platform_id: str, uid: str) -> dict | None:
    for a in await load_accounts(platform_id):
        if a.get("uid") == uid:
            return a
    return None


async def upsert_account(platform_id: str, entry: dict, *, preserve: bool = True) -> dict:
    """按 uid 去重新增/覆盖；preserve=True 保留旧 checkin/quota 缓存（重登录场景）。"""
    async with _get_locks(platform_id)[0]:
        accounts = await asyncio.to_thread(_load_sync, platform_id)
        uid = entry.get("uid")
        merged = dict(entry)
        for i, old in enumerate(accounts):
            if old.get("uid") == uid:
                if preserve:
                    for key in ("checkin", "quota"):
                        if key not in merged and old.get(key):
                            merged[key] = old[key]
                accounts[i] = merged
                break
        else:
            accounts.append(merged)
        await asyncio.to_thread(_save_sync, platform_id, accounts)
    return merged


async def update_account(platform_id: str, uid: str, patch: dict) -> dict:
    """局部更新（checkin/quota 刷新等）；uid 不存在抛 KeyError。"""
    async with _get_locks(platform_id)[0]:
        accounts = await asyncio.to_thread(_load_sync, platform_id)
        for i, old in enumerate(accounts):
            if old.get("uid") == uid:
                accounts[i] = {**old, **patch}
                await asyncio.to_thread(_save_sync, platform_id, accounts)
                return accounts[i]
    raise KeyError(uid)


async def delete_account(platform_id: str, uid: str) -> bool:
    async with _get_locks(platform_id)[0]:
        accounts = await asyncio.to_thread(_load_sync, platform_id)
        remaining = [a for a in accounts if a.get("uid") != uid]
        if len(remaining) == len(accounts):
            return False
        await asyncio.to_thread(_save_sync, platform_id, remaining)
        return True
