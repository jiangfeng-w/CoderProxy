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
