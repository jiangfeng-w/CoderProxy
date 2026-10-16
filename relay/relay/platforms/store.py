"""平台多账号 JSON 存储：platform_{platform_id}.json，与 relay_state.json 完全隔离。

结构（占位符示例，不含真实凭证）：
{"version": 1, "accounts": [{"uid", "nickname", "email", "enterprise_id", "domain",
  "access_token", "refresh_token", "expires_at", "status",
  "checkin": {"last_checkin_date", "streak_days", "today_checked_in"},
  "quota": {"total", "remaining", "used", "expire_at", "fetched_at"}}]}

- 落盘纪律照抄 relay/storage.py（双锁 + asyncio.to_thread + tmp.replace 原子写），
  但按 platform_id 持**独立锁实例**，多平台并存互不阻塞。
- upsert 按 uid 去重；重登录覆盖 token 但**保留旧 checkin/quota 缓存**（preserve）；
  同批保留用户级/健康态字段（priority / enabled / fail_count / degraded_until /
  model_cooldowns，见「三需求同批」注记）。
- expires_at 统一毫秒（秒/毫秒双口径归一：0 < v < 1e11 视为秒 ×1000）。
- today_checked_in 读取时按 Asia/Shanghai（固定 UTC+8，无夏令时）本地日期失效：
  last_checkin_date != 今天 → 视为未签到（内存修正，不落盘）。

三需求同批（2026-10-17 一次改完，三份 spec 共享本模块改动点）：

- 《WorkBuddy 反代账号池健康管理》：fail_count / degraded_until / model_cooldowns
  字段 + note_failure / note_success / note_rate_limit / note_model_missing /
  clear_model_missing 帮助函数（原子读改写）；
- 《WorkBuddy 反代账号优先级》：priority 字段（int，1 起，越小越优先，缺省 1）；
- 《WorkBuddy 账号启用停用》：enabled 字段（用户级开关，与系统态 status 正交）
  + is_selectable 统一读口径（全链路排除点都改读它）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from relay.config import settings

logger = logging.getLogger(__name__)

# Asia/Shanghai 固定 UTC+8（无夏令时，等价且零 tzdata 依赖）
CN_TZ = timezone(timedelta(hours=8))

# ── 健康管理参数（自有取值，非 panel/9router 口径；见健康管理 spec §2/§3）──
DEGRADE_FAIL_THRESHOLD = 3  # 连败阈值（panel 5 连败；桌面场景取 3——快速挂起）
DEGRADE_WINDOW_MS = 5 * 60_000  # 挂起时长 5min（panel 10min）
RATE_LIMIT_BACKOFF_BASE_MS = 60_000  # 6004 无恢复时间时的退避基数（9router 2s / panel 600s）
RATE_LIMIT_BACKOFF_CAP_MS = 15 * 60_000  # 退避封顶（panel 2h）
RATE_LIMIT_MAX_UNTIL_MS = 12 * 3600_000  # 上游恢复时间宽松封顶（防脏数据；2026-10-15 用户拍板）
MODEL_MISSING_TTL_MS = 10 * 60_000  # 11102 负缓存 TTL（panel 6h 起——桌面取短 TTL）


def now_ms() -> int:
    """当前毫秒时间戳（独立函数：测试 monkeypatch 注入时钟的唯一入口）。"""
    return int(time.time() * 1000)


def is_selectable(account: dict) -> bool:
    """选号读口径（启用停用 spec 统一助手）：系统态 normal 且用户未停用。

    `enabled` 缺失/null/非 False 一律视为启用（存量文件零迁移）；
    与 `status` 正交——系统自动逻辑（挂起/冷却）永不写 enabled。
    """
    if str(account.get("status") or "normal") != "normal":
        return False
    return account.get("enabled") is not False


def is_healthy_for_model(account: dict, model: str, *, now: int | None = None) -> bool:
    """健康过滤（健康管理 spec §3 选号链）：非降权 ∩ 非（uid, model）冷却。"""
    ts = now if now is not None else now_ms()
    if int(account.get("degraded_until") or 0) > ts:
        return False
    cooldowns = account.get("model_cooldowns")
    if isinstance(cooldowns, dict):
        entry = cooldowns.get(str(model or ""))
        if isinstance(entry, dict) and int(entry.get("until") or 0) > ts:
            return False
    return True


def model_cooldown_until(account: dict, model: str, *, now: int | None = None) -> int:
    """该账号对某模型的冷却截止（未命中/已过期 → 0）。"""
    ts = now if now is not None else now_ms()
    cooldowns = account.get("model_cooldowns")
    if not isinstance(cooldowns, dict):
        return 0
    entry = cooldowns.get(str(model or ""))
    if not isinstance(entry, dict):
        return 0
    until = int(entry.get("until") or 0)
    return until if until > ts else 0

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
    """按 uid 去重新增/覆盖；preserve=True 保留旧 checkin/quota 缓存（重登录场景）。

    三需求同批（2026-10-17）：preserve 名单一次加全
    `priority + fail_count + degraded_until + model_cooldowns + enabled`——
    重登录/导入不清用户设置（优先级/停用）与健康态（降权/冷却）。
    """
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
                    # 数值/字典字段按真值保留（0/{} 与缺省等价，无信息量）
                    for key in ("priority", "fail_count", "degraded_until",
                                "model_cooldowns"):
                        if key not in merged and old.get(key):
                            merged[key] = old[key]
                    # enabled 显式判在场而非真值：False 是 falsy，直接进上面的
                    # 真值循环会导致「停用号重登录后被静默启用」（spec 关键陷阱）
                    if "enabled" not in merged and "enabled" in old:
                        merged["enabled"] = old["enabled"]
                accounts[i] = merged
                break
        else:
            accounts.append(merged)
        await asyncio.to_thread(_save_sync, platform_id, accounts)
    return merged


async def _mutate_account(platform_id: str, uid: str, mutate) -> dict | None:
    """锁内读改写单账号（健康帮助函数共用原子原语）；uid 不存在返回 None。

    喂入口纪律：调用方（chat 链路）不得因账号已被删除而中断主链路。
    """
    async with _get_locks(platform_id)[0]:
        accounts = await asyncio.to_thread(_load_sync, platform_id)
        for i, old in enumerate(accounts):
            if old.get("uid") == uid:
                updated = mutate(dict(old))
                accounts[i] = updated
                await asyncio.to_thread(_save_sync, platform_id, accounts)
                return updated
    return None


async def note_failure(platform_id: str, uid: str, *, now: int | None = None) -> None:
    """连败计数 + 达阈软降级（健康管理 spec §2；只喂「不知道原因的失败」）。

    fail_count++；达阈值 3 → degraded_until = now + 5min、fail_count 清零。
    降权期内再次达阈**不延长**（对齐 panel degrade.go：防用户重试把降权越堆越厚；
    计数清零，到期放行后需重新连败满阈值才再降）。
    """
    ts = now if now is not None else now_ms()

    def _mutate(acc: dict) -> dict:
        fail_count = int(acc.get("fail_count") or 0) + 1
        degraded_until = int(acc.get("degraded_until") or 0)
        if fail_count >= DEGRADE_FAIL_THRESHOLD:
            if degraded_until <= ts:
                degraded_until = ts + DEGRADE_WINDOW_MS
            fail_count = 0
        return {**acc, "fail_count": fail_count, "degraded_until": degraded_until}

    await _mutate_account(platform_id, uid, _mutate)


async def note_success(platform_id: str, uid: str) -> None:
    """成功清零连败与挂起（§2：note_success 时机 = 流正常收尾）。"""
    def _mutate(acc: dict) -> dict:
        return {**acc, "fail_count": 0, "degraded_until": 0}

    await _mutate_account(platform_id, uid, _mutate)


async def note_rate_limit(platform_id: str, uid: str, model: str, *,
                          reset_at_ms: int | None = None,
                          now: int | None = None) -> int:
    """6004 模型级冷却（原子读改写）；返回最终 until（ms）。

    - 已有未到期条目 → 不翻倍不延长（返回原 until）；
    - `reset_at_ms`（上游恢复墙钟）有效 → 锁满（宽松封顶 12h 防脏数据；
      2026-10-15 用户拍板「尊重上游时间」——实测撞出 8h14m 重置）；
    - 否则 → 有界退避 60s × 2^(hits-1)，封顶 15min。
    hits 在每次新建条目时累加（跨冷却期退避指数递增）；冷却期内触发不增。
    """
    ts = now if now is not None else now_ms()
    name = str(model or "")

    def _mutate(acc: dict) -> dict:
        cooldowns = dict(acc.get("model_cooldowns") or {})
        entry = cooldowns.get(name)
        if isinstance(entry, dict) and int(entry.get("until") or 0) > ts:
            return acc  # 冷却期内：不翻倍不延长（零改动）
        hits = int(entry.get("hits") or 0) + 1 if isinstance(entry, dict) else 1
        if reset_at_ms and reset_at_ms > ts:
            until = min(int(reset_at_ms), ts + RATE_LIMIT_MAX_UNTIL_MS)
        else:
            backoff = min(RATE_LIMIT_BACKOFF_BASE_MS * (2 ** (hits - 1)),
                          RATE_LIMIT_BACKOFF_CAP_MS)
            until = ts + backoff
        cooldowns[name] = {"until": until, "reason": "6004", "hits": hits}
        return {**acc, "model_cooldowns": cooldowns}

    updated = await _mutate_account(platform_id, uid, _mutate)
    if updated is None:
        return 0
    entry = (updated.get("model_cooldowns") or {}).get(name)
    return int(entry.get("until") or 0) if isinstance(entry, dict) else 0


async def note_model_missing(platform_id: str, uid: str, model: str, *,
                             ttl_ms: int | None = None,
                             now: int | None = None) -> int:
    """11102 模型不可用负缓存（TTL 10min；原子读改写）；返回 until（ms）。"""
    ts = now if now is not None else now_ms()
    ttl = MODEL_MISSING_TTL_MS if ttl_ms is None else int(ttl_ms)
    name = str(model or "")

    def _mutate(acc: dict) -> dict:
        cooldowns = dict(acc.get("model_cooldowns") or {})
        cooldowns[name] = {"until": ts + ttl, "reason": "11102"}
        return {**acc, "model_cooldowns": cooldowns}

    updated = await _mutate_account(platform_id, uid, _mutate)
    if updated is None:
        return 0
    entry = (updated.get("model_cooldowns") or {}).get(name)
    return int(entry.get("until") or 0) if isinstance(entry, dict) else 0


async def clear_model_missing(platform_id: str, uid: str, model: str) -> None:
    """成功一次即清该模型的 11102 条目（6004 条目不清——各有恢复语义）。"""
    name = str(model or "")

    def _mutate(acc: dict) -> dict:
        cooldowns = acc.get("model_cooldowns")
        if not isinstance(cooldowns, dict):
            return acc
        entry = cooldowns.get(name)
        if isinstance(entry, dict) and entry.get("reason") == "11102":
            return {**acc, "model_cooldowns": {k: v for k, v in cooldowns.items()
                                               if k != name}}
        return acc

    await _mutate_account(platform_id, uid, _mutate)


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
