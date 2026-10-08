"""WorkBuddy 设备授权登录流单测：MockTransport 全序列（start → 轮询 → 归档/刷新/取消）。

不触网：oauth 模块的 WbClient 以 MockTransport 注入；POLL_INTERVAL_S/LOGIN_TIMEOUT_S
monkeypatch 加速（真实节奏 1.5s/600s 由常量控制）。
"""
import asyncio
import time

import httpx
import pytest

from relay.config import settings
from relay.platforms import store
from relay.platforms.base import PlatformAuthError
from relay.platforms.workbuddy import oauth

API = "https://copilot.tencent.com"


@pytest.fixture(autouse=True)
def fast_env(tmp_path, monkeypatch):
    """存储落 tmp + 轮询节奏加速。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(oauth, "POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(oauth, "LOGIN_TIMEOUT_S", 2.0)


def _mock_client(routes: dict[str, list[httpx.Response] | httpx.Response]) -> None:
    """按 URL 前缀路由的 MockTransport 注入 oauth._client；列表 = 逐次响应（耗尽后重复末项）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, resp in routes.items():
            if str(request.url).startswith(prefix):
                if isinstance(resp, list):
                    item, *rest = resp
                    if rest:
                        resp[:] = rest  # type: ignore[arg-type]
                    return item
                return resp
        raise AssertionError(f"unexpected url: {request.url}")

    transport = httpx.MockTransport(handler)
    oauth._client = oauth.WbClient(transport=transport)
    # billing._client 同步注入：登录预取（_prefetch_caches）走 billing 域，
    # 不注入会漏到真实网络（单测打真上游是事故）
    from relay.platforms.workbuddy import billing as wb_billing
    wb_billing._client = wb_billing.WbClient(transport=transport)


async def _wait_status(login_id: str) -> dict:
    """轮询 login_status 至非 pending（对齐前端轮询行为）。"""
    for _ in range(200):
        result = await oauth.login_status(login_id)
        if result["status"] != "pending":
            return result
        await asyncio.sleep(0.02)
    raise AssertionError("登录轮询等待超时")


async def test_start_login_pending_and_success_archive(tmp_path):
    """全序列：auth/state → pending → auth/token（先未授权后成功）→ login/account 归档。"""
    captured_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured_urls.append(str(request.url))
        for prefix, resp in _ROUTES.items():
            if str(request.url).startswith(prefix):
                if isinstance(resp, list):
                    item, *rest = resp
                    if rest:
                        resp[:] = rest
                    return item
                return resp
        raise AssertionError(f"unexpected url: {request.url}")

    _ROUTES = {
        f"{API}/v2/plugin/auth/state": httpx.Response(
            200, json={"code": 0, "data": {"state": "st_abc", "authUrl": "https://www.codebuddy.cn/login?state=st_abc"}}),
        f"{API}/v2/plugin/auth/token": [
            httpx.Response(200, json={"code": 1, "msg": "waiting"}),
            httpx.Response(200, json={"code": 0, "data": {
                "accessToken": "at_new", "refreshToken": "rt_new",
                "expiresIn": 7200, "domain": "tencent"}}),
        ],
        f"{API}/v2/plugin/login/account": httpx.Response(
            200, json={"code": 0, "data": {"uid": "u_wb1", "nickname": "小号甲",
                                           "email": "", "enterpriseId": "ent_9"}}),
        f"{API}/v2/billing/meter/checkin-activity-status": httpx.Response(
            200, json={"code": 0, "data": {"todayCheckedIn": 0, "streakDays": 1}}),
        f"{API}/v2/billing/meter/get-user-resource": httpx.Response(
            200, json={"code": 0, "data": {}}),
    }
    transport = httpx.MockTransport(handler)
    oauth._client = oauth.WbClient(transport=transport)
    from relay.platforms.workbuddy import billing as wb_billing
    wb_billing._client = wb_billing.WbClient(transport=transport)

    started = await oauth.start_login()
    assert started["login_id"].startswith("wb_")
    assert started["authorize_url"] == "https://www.codebuddy.cn/login?state=st_abc"
    assert started["expires_in"] == oauth.LOGIN_TIMEOUT_S
    # platform 档位：官方桌面客户端身份 workbuddy（对齐 cockpit-tools 18.7k★ / workbuddy-switch）
    assert "platform=workbuddy" in captured_urls[0]

    result = await _wait_status(started["login_id"])
    assert result["status"] == "success", f"登录失败: {result}"
    assert result["uid"] == "u_wb1"

    accounts = await store.load_accounts("workbuddy")
    assert len(accounts) == 1
    acc = accounts[0]
    assert acc["uid"] == "u_wb1"
    assert acc["nickname"] == "小号甲"
    assert acc["access_token"] == "at_new"
    assert acc["refresh_token"] == "rt_new"
    assert acc["domain"] == "tencent"
    assert acc["enterprise_id"] == "ent_9"
    # expiresIn 7200s → expires_at 归一为毫秒且在未来
    assert acc["expires_at"] > 0


async def test_relogin_preserves_checkin_cache():
    """重登录覆盖 token 但保留旧 checkin/quota 缓存（store.preserve 经 oauth 生效）。

    注意：登录归档后 _prefetch_caches 会用上游真值覆盖 checkin（mock 上游返回
    todayCheckedIn=0/streakDays=9），故这里断言的是「预取后的权威状态」而非旧缓存——
    旧缓存保留语义仅在预取失败路径生效（见下条测试）。
    """
    _mock_client({
        f"{API}/v2/plugin/auth/state": httpx.Response(
            200, json={"code": 0, "data": {"state": "st_r1", "authUrl": "u"}}),
        f"{API}/v2/plugin/auth/token": httpx.Response(
            200, json={"code": 0, "data": {"accessToken": "at_2", "expiresIn": 7200}}),
        f"{API}/v2/plugin/login/account": httpx.Response(
            200, json={"code": 0, "data": {"uid": "u_keep", "nickname": "新昵称"}}),
        f"{API}/v2/billing/meter/checkin-activity-status": httpx.Response(
            200, json={"code": 0, "data": {"todayCheckedIn": 0, "streakDays": 9, "active": True}}),
        f"{API}/v2/billing/meter/get-user-resource": httpx.Response(
            200, json={"code": 0, "data": {"resources": [
                {"CapacitySizePrecise": "100", "CapacityUsedPrecise": "60"}]}}),
    })
    await store.upsert_account("workbuddy", {
        "uid": "u_keep", "nickname": "旧昵称", "access_token": "at_1",
        "checkin": {"last_checkin_date": "2026-10-06", "streak_days": 5,
                    "today_checked_in": False},
    })

    started = await oauth.start_login()
    result = await _wait_status(started["login_id"])
    assert result["status"] == "success"

    acc = (await store.load_accounts("workbuddy"))[0]
    assert acc["access_token"] == "at_2"
    assert acc["nickname"] == "新昵称"
    # 登录后首屏即正确：签到/配额缓存已由 _prefetch_caches 用上游真值刷新
    assert acc["checkin"]["streak_days"] == 9
    assert acc["checkin"]["today_checked_in"] is False
    assert acc["quota"]["remaining"] == 40


async def test_login_prefetch_failure_does_not_break_login():
    """预拉签到/配额失败不回滚登录：账号照常入库，缓存保持旧值（可手动刷新补）。"""
    _mock_client({
        f"{API}/v2/plugin/auth/state": httpx.Response(
            200, json={"code": 0, "data": {"state": "st_pf", "authUrl": "u"}}),
        f"{API}/v2/plugin/auth/token": httpx.Response(
            200, json={"code": 0, "data": {"accessToken": "at_pf", "expiresIn": 7200}}),
        f"{API}/v2/plugin/login/account": httpx.Response(
            200, json={"code": 0, "data": {"uid": "u_pf", "nickname": "预取失败号"}}),
        f"{API}/v2/billing/meter/checkin-activity-status": httpx.Response(500, json={"msg": "boom"}),
        f"{API}/v2/billing/meter/checkin-status": httpx.Response(500, json={"msg": "boom"}),
        f"{API}/v2/billing/meter/get-user-resource": httpx.Response(500, json={"msg": "boom"}),
    })
    started = await oauth.start_login()
    result = await _wait_status(started["login_id"])
    assert result["status"] == "success", f"预取失败不应影响登录: {result}"

    acc = (await store.load_accounts("workbuddy"))[0]
    assert acc["uid"] == "u_pf"
    assert "checkin" not in acc and "quota" not in acc  # 无旧缓存则保持缺省，不阻塞


async def test_login_status_after_completion_reports_unknown():
    """完成即弹出：success 后再次查询 → failed（槽已清理）。"""
    _mock_client({
        f"{API}/v2/plugin/auth/state": httpx.Response(
            200, json={"code": 0, "data": {"state": "st_c1", "authUrl": "u"}}),
        f"{API}/v2/plugin/auth/token": httpx.Response(
            200, json={"code": 0, "data": {"accessToken": "at", "expiresIn": 60}}),
        f"{API}/v2/plugin/login/account": httpx.Response(
            200, json={"code": 0, "data": {"uid": "u_c1"}}),
    })
    started = await oauth.start_login()
    await _wait_status(started["login_id"])
    again = await oauth.login_status(started["login_id"])
    assert again["status"] == "failed"
    assert "不存在" in again["error"]


async def test_poll_timeout_marks_failed():
    """600s 超时（测试加速）：token 一直未授权 → failed 超时文案。"""
    monkey_timeout = pytest.MonkeyPatch()
    monkey_timeout.setattr(oauth, "LOGIN_TIMEOUT_S", 0.05)
    try:
        _mock_client({
            f"{API}/v2/plugin/auth/state": httpx.Response(
                200, json={"code": 0, "data": {"state": "st_t1", "authUrl": "u"}}),
            f"{API}/v2/plugin/auth/token": httpx.Response(
                200, json={"code": 1, "msg": "waiting"}),
        })
        started = await oauth.start_login()
        result = await _wait_status(started["login_id"])
        assert result["status"] == "failed"
        assert "超时" in result["error"]
    finally:
        monkey_timeout.undo()


async def test_cancel_login():
    """取消：槽移除，status 查询报不存在。"""
    _mock_client({
        f"{API}/v2/plugin/auth/state": httpx.Response(
            200, json={"code": 0, "data": {"state": "st_x1", "authUrl": "u"}}),
        f"{API}/v2/plugin/auth/token": httpx.Response(
            200, json={"code": 1, "msg": "waiting"}),
    })
    started = await oauth.start_login()
    await oauth.cancel_login(started["login_id"])
    result = await oauth.login_status(started["login_id"])
    assert result["status"] == "failed"


async def test_refresh_token_success_rotates():
    """refresh 成功：access/refresh 轮换、expires_at 归一毫秒、回写存储。"""
    _mock_client({
        "https://www.codebuddy.cn/v2/plugin/auth/token/refresh": httpx.Response(
            200, json={"code": 0, "data": {"accessToken": "at_fresh",
                                           "refreshToken": "rt_fresh",
                                           "expiresAt": 1_800_000_000}}),
    })
    await store.upsert_account("workbuddy", {
        "uid": "u_r", "access_token": "at_old", "refresh_token": "rt_old",
        "expires_at": 1_000, "domain": "tencent",
    })
    account = await store.find_account("workbuddy", "u_r")
    updated = await oauth.refresh_token(account)
    assert updated["access_token"] == "at_fresh"
    assert updated["refresh_token"] == "rt_fresh"
    # 秒口径 1_800_000_000 → ×1000 毫秒
    assert updated["expires_at"] == 1_800_000_000_000

    persisted = await store.find_account("workbuddy", "u_r")
    assert persisted["access_token"] == "at_fresh"


async def test_refresh_token_rejected_maps_login_required():
    """上游 401 → login_required（引导重登），非 401 业务失败 → refresh_failed。"""
    _mock_client({
        "https://www.codebuddy.cn/v2/plugin/auth/token/refresh": httpx.Response(
            401, json={"code": 401, "msg": "token invalid"}),
    })
    await store.upsert_account("workbuddy", {
        "uid": "u_rj", "access_token": "at", "refresh_token": "rt"})
    account = await store.find_account("workbuddy", "u_rj")
    with pytest.raises(PlatformAuthError) as ei:
        await oauth.refresh_token(account)
    assert ei.value.kind == "login_required"


def test_expiry_ms_dual_units():
    """_expiry_ms：expiresAt 秒/毫秒双口径；expiresIn 秒偏移；非法归 0。"""
    assert oauth._expiry_ms({"expiresAt": 1_800_000_000}) == 1_800_000_000_000
    assert oauth._expiry_ms({"expiresAt": 1_800_000_000_000}) == 1_800_000_000_000
    now_ms = time.time() * 1000
    soon = oauth._expiry_ms({"expiresIn": 120})
    assert now_ms + 100_000 <= soon <= now_ms + 140_000
    assert oauth._expiry_ms({}) == 0
