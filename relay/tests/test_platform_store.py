"""平台插件底座单测：多账号 JSON 存储（platform_{id}.json）的隔离/去重/日期失效语义。

不触发网络：storage 落 tmp 目录（monkeypatch settings.data_dir）。
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from relay.config import settings
from relay.platforms import store as platform_store


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    return tmp_path


def _account(uid="u_1", **overrides):
    entry = {
        "uid": uid,
        "nickname": f"昵称{uid}",
        "email": "",
        "domain": "",
        "access_token": "at_xxx",
        "refresh_token": "rt_xxx",
        "expires_at": 1_800_000_000_000,
        "status": "normal",
    }
    entry.update(overrides)
    return entry


async def test_upsert_creates_and_dedups(data_dir):
    """upsert 按 uid 去重：两次写入同 uid 只留一行。"""
    await platform_store.upsert_account("workbuddy", _account())
    await platform_store.upsert_account("workbuddy", _account(nickname="改名"))
    accounts = await platform_store.load_accounts("workbuddy")
    assert len(accounts) == 1
    assert accounts[0]["nickname"] == "改名"


async def test_upsert_preserves_checkin_quota_cache(data_dir):
    """重登录覆盖 token 但保留旧 checkin/quota 缓存（preserve=True 默认）。"""
    await platform_store.upsert_account("workbuddy", _account(
        checkin={"last_checkin_date": "2026-10-06", "streak_days": 3, "today_checked_in": False},
        quota={"total": 100, "remaining": 50, "used": 50, "expire_at": "", "fetched_at": "t1"}))
    await platform_store.upsert_account("workbuddy", _account(access_token="at_new"))

    acc = (await platform_store.load_accounts("workbuddy"))[0]
    assert acc["access_token"] == "at_new"
    assert acc["checkin"]["streak_days"] == 3
    assert acc["quota"]["remaining"] == 50

    # preserve=False：全量覆盖（显式清除缓存时用）
    await platform_store.upsert_account("workbuddy", _account(uid="u_1"), preserve=False)
    acc = (await platform_store.load_accounts("workbuddy"))[0]
    assert "checkin" not in acc and "quota" not in acc


async def test_update_and_delete(data_dir):
    await platform_store.upsert_account("workbuddy", _account())
    updated = await platform_store.update_account(
        "workbuddy", "u_1",
        {"checkin": {"last_checkin_date": "2026-10-07", "streak_days": 1, "today_checked_in": True}})
    assert updated["checkin"]["streak_days"] == 1

    with pytest.raises(KeyError):
        await platform_store.update_account("workbuddy", "u_missing", {"status": "x"})

    assert await platform_store.delete_account("workbuddy", "u_1") is True
    assert await platform_store.delete_account("workbuddy", "u_1") is False
    assert await platform_store.load_accounts("workbuddy") == []


def test_normalize_expires_ms_dual_units():
    """秒/毫秒双口径归一：秒 ×1000，毫秒保留，非法归 0。"""
    assert platform_store.normalize_expires_ms(1_800_000_000) == 1_800_000_000_000
    assert platform_store.normalize_expires_ms("1800000000") == 1_800_000_000_000
    assert platform_store.normalize_expires_ms(1_800_000_000_000) == 1_800_000_000_000
    assert platform_store.normalize_expires_ms(0) == 0
    assert platform_store.normalize_expires_ms(None) == 0
    assert platform_store.normalize_expires_ms("abc") == 0


async def test_today_checked_in_invalidates_on_read(data_dir):
    """读取时按 Asia/Shanghai 本地日期失效：last_checkin_date != 今天 → 未签到。"""
    today = platform_store.today_cn()
    yesterday = (datetime.now(platform_store.CN_TZ) - timedelta(days=1)).strftime("%Y-%m-%d")

    await platform_store.upsert_account("workbuddy", _account(
        uid="u_old",
        checkin={"last_checkin_date": yesterday, "streak_days": 2, "today_checked_in": True}))
    await platform_store.upsert_account("workbuddy", _account(
        uid="u_today",
        checkin={"last_checkin_date": today, "streak_days": 2, "today_checked_in": True}))

    by_uid = {a["uid"]: a for a in await platform_store.load_accounts("workbuddy")}
    assert by_uid["u_old"]["checkin"]["today_checked_in"] is False
    assert by_uid["u_today"]["checkin"]["today_checked_in"] is True

    # 落盘原始值不被读取路径改写（内存修正，不落盘）
    raw = json.loads(Path(data_dir, "platform_workbuddy.json").read_text("utf-8"))
    raw_old = next(a for a in raw["accounts"] if a["uid"] == "u_old")
    assert raw_old["checkin"]["today_checked_in"] is True


async def test_platform_files_isolated(data_dir):
    """按 platform 分文件且互不干扰；不写 relay_state.json。"""
    await platform_store.upsert_account("workbuddy", _account())
    await platform_store.upsert_account("trae", _account(uid="u_trae"))

    assert Path(data_dir, "platform_workbuddy.json").exists()
    assert Path(data_dir, "platform_trae.json").exists()
    assert not Path(data_dir, "relay_state.json").exists()
    assert len(await platform_store.load_accounts("workbuddy")) == 1
    assert [a["uid"] for a in await platform_store.load_accounts("trae")] == ["u_trae"]


async def test_corrupted_file_treated_empty(data_dir):
    """存储文件损坏按空账号处理（与 storage.py 纪律一致）。"""
    Path(data_dir, "platform_workbuddy.json").write_text("not-json{", "utf-8")
    assert await platform_store.load_accounts("workbuddy") == []
    await platform_store.upsert_account("workbuddy", _account())
    assert len(await platform_store.load_accounts("workbuddy")) == 1


# ─────────── 三需求同批（2026-10-17）：优先级 / 启用停用 / 健康字段 ───────────


async def test_priority_preserve_and_patch(data_dir):
    """priority：缺省不落盘（读取侧缺省 1）；重登录保留用户设置；update 局部 patch。"""
    await platform_store.upsert_account("workbuddy", _account())
    acc = (await platform_store.load_accounts("workbuddy"))[0]
    assert "priority" not in acc  # 缺省语义在读取侧（不写盘放大）
    await platform_store.update_account("workbuddy", "u_1", {"priority": 5})
    await platform_store.upsert_account("workbuddy", _account(access_token="at_new"))
    acc = (await platform_store.load_accounts("workbuddy"))[0]
    assert acc["priority"] == 5  # 重登录不清优先级


async def test_enabled_falsy_preserve(data_dir):
    """falsy 陷阱回归：enabled=False 重登录后必须保留（不能静默启用）。"""
    await platform_store.upsert_account("workbuddy", _account(enabled=False))
    await platform_store.upsert_account("workbuddy", _account(access_token="at_new"))
    acc = (await platform_store.load_accounts("workbuddy"))[0]
    assert acc["enabled"] is False


async def test_health_fields_preserve(data_dir):
    """fail_count / degraded_until / model_cooldowns 重登录后保留（不清惩罚与冷却）。"""
    await platform_store.upsert_account("workbuddy", _account(
        fail_count=2, degraded_until=1_234_567,
        model_cooldowns={"glm-5.3": {"until": 999_999, "reason": "6004", "hits": 1}}))
    await platform_store.upsert_account("workbuddy", _account(access_token="at_new"))
    acc = (await platform_store.load_accounts("workbuddy"))[0]
    assert acc["fail_count"] == 2
    assert acc["degraded_until"] == 1_234_567
    assert acc["model_cooldowns"]["glm-5.3"]["reason"] == "6004"


def test_is_selectable():
    """统一读口径：enabled 缺失/null 视为启用；停用与系统态正交。"""
    assert platform_store.is_selectable({"status": "normal"}) is True
    assert platform_store.is_selectable({"status": "normal", "enabled": True}) is True
    assert platform_store.is_selectable({"status": "normal", "enabled": None}) is True
    assert platform_store.is_selectable({"status": "normal", "enabled": False}) is False
    assert platform_store.is_selectable({"status": "disabled"}) is False
    assert platform_store.is_selectable({"status": "disabled", "enabled": True}) is False


def test_is_healthy_for_model():
    """健康过滤：降权 ∩ (uid, model) 冷却；其他模型不受影响。"""
    now = 1_000_000_000_000
    assert platform_store.is_healthy_for_model({}, "m", now=now) is True
    assert platform_store.is_healthy_for_model(
        {"degraded_until": now + 1}, "m", now=now) is False
    assert platform_store.is_healthy_for_model(
        {"degraded_until": now - 1}, "m", now=now) is True  # 已到期
    assert platform_store.is_healthy_for_model(
        {"model_cooldowns": {"m": {"until": now + 1, "reason": "6004"}}}, "m",
        now=now) is False
    assert platform_store.is_healthy_for_model(
        {"model_cooldowns": {"m": {"until": now + 1}}}, "other", now=now) is True


async def test_note_failure_degrades_and_success_resets(data_dir):
    """连败软降级：3 连败挂起 5min（计数清零）；降权期内达阈不延长；成功清零。"""
    await platform_store.upsert_account("workbuddy", _account())
    now = 1_000_000_000_000
    await platform_store.note_failure("workbuddy", "u_1", now=now)
    await platform_store.note_failure("workbuddy", "u_1", now=now)
    acc = (await platform_store.load_accounts("workbuddy"))[0]
    assert acc["fail_count"] == 2 and not acc.get("degraded_until")
    await platform_store.note_failure("workbuddy", "u_1", now=now)
    acc = (await platform_store.load_accounts("workbuddy"))[0]
    assert acc["fail_count"] == 0
    assert acc["degraded_until"] == now + platform_store.DEGRADE_WINDOW_MS
    # 降权期内再次达阈：不延长（防用户重试把降权越堆越厚）
    for _ in range(3):
        await platform_store.note_failure("workbuddy", "u_1", now=now + 1000)
    acc = (await platform_store.load_accounts("workbuddy"))[0]
    assert acc["degraded_until"] == now + platform_store.DEGRADE_WINDOW_MS
    # 成功清零
    await platform_store.note_success("workbuddy", "u_1")
    acc = (await platform_store.load_accounts("workbuddy"))[0]
    assert acc["fail_count"] == 0 and acc["degraded_until"] == 0


async def test_health_notes_missing_account_silent(data_dir):
    """喂入口纪律：账号已被删 → 静默忽略（不炸主链路）。"""
    await platform_store.note_failure("workbuddy", "u_missing")
    await platform_store.note_success("workbuddy", "u_missing")
    await platform_store.note_rate_limit("workbuddy", "u_missing", "m")
    await platform_store.clear_model_missing("workbuddy", "u_missing", "m")


async def test_note_rate_limit_backoff_reset_and_cap(data_dir):
    """6004：有界退避 60s×2^(hits-1)；冷却期内不延长；恢复时间锁满（12h 宽松封顶）。"""
    await platform_store.upsert_account("workbuddy", _account())
    now = 1_000_000_000_000
    until1 = await platform_store.note_rate_limit("workbuddy", "u_1", "m", now=now)
    assert until1 == now + 60_000  # hits=1 → 60s
    until2 = await platform_store.note_rate_limit("workbuddy", "u_1", "m", now=now + 1000)
    assert until2 == until1  # 冷却期内：不翻倍不延长
    until3 = await platform_store.note_rate_limit("workbuddy", "u_1", "m", now=until1 + 1)
    assert until3 == until1 + 1 + 120_000  # hits=2 → 120s
    # 恢复时间（8h14m 实测形态）→ 锁满（尊重上游时间）
    reset_at = until3 + 8 * 3600_000 + 14 * 60_000
    until4 = await platform_store.note_rate_limit("workbuddy", "u_1", "m",
                                                  reset_at_ms=reset_at, now=until3 + 1)
    assert until4 == reset_at
    # 脏数据（100h）→ 宽松封顶 12h
    until5 = await platform_store.note_rate_limit("workbuddy", "u_1", "m",
                                                  reset_at_ms=until4 + 100 * 3600_000,
                                                  now=until4 + 1)
    assert until5 == until4 + 1 + platform_store.RATE_LIMIT_MAX_UNTIL_MS


async def test_note_model_missing_and_clear(data_dir):
    """11102 负缓存 10min；成功清除只清 11102（6004 条目不清——各有恢复语义）。"""
    await platform_store.upsert_account("workbuddy", _account())
    now = 1_000_000_000_000
    until = await platform_store.note_model_missing("workbuddy", "u_1", "m", now=now)
    assert until == now + platform_store.MODEL_MISSING_TTL_MS
    await platform_store.note_rate_limit("workbuddy", "u_1", "m2", now=now)
    await platform_store.clear_model_missing("workbuddy", "u_1", "m")
    acc = (await platform_store.load_accounts("workbuddy"))[0]
    cds = acc["model_cooldowns"]
    assert "m" not in cds
    assert cds["m2"]["reason"] == "6004"  # 6004 条目保留
