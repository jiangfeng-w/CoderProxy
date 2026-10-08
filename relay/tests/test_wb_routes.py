"""WorkBuddy /v1/platforms/* 管理端点单测：注册表分发、错误映射、安全断言、ta3 回归。

不触网：oauth/billing 模块的 WbClient 以 MockTransport 注入；存储落 tmp。
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from relay.config import settings
from relay.platforms import store
from relay.platforms.workbuddy import billing, oauth
from relay.routes import app as relay_app

API = "https://copilot.tencent.com"
METER = f"{API}/v2/billing/meter"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    # 轮询节奏加速：默认 1.5s 间隔在同步 TestClient 轮询里等不到
    monkeypatch.setattr(oauth, "POLL_INTERVAL_S", 0.01)
    settings.relay_api_key = "gate-test-key"
    with TestClient(relay_app, raise_server_exceptions=False) as c:
        yield c
    settings.relay_api_key = ""


def _auth():
    return {"Authorization": "Bearer gate-test-key"}


def _mock_wb(routes: dict[str, httpx.Response]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        for prefix, resp in routes.items():
            if str(request.url).startswith(prefix):
                return resp
        raise AssertionError(f"unexpected url: {request.url}")

    transport = httpx.MockTransport(handler)
    oauth._client = oauth.WbClient(transport=transport)
    billing._client = billing.WbClient(transport=transport)


# ─────────────────────────── 平台注册表 ───────────────────────────

def test_list_platforms(client):
    r = client.get("/v1/platforms", headers=_auth())
    assert r.status_code == 200
    platforms = r.json()["platforms"]
    ids = [p["id"] for p in platforms]
    assert "workbuddy" in ids
    wb = next(p for p in platforms if p["id"] == "workbuddy")
    assert wb["supports_checkin"] is True
    assert wb["supports_quota"] is True


def test_unknown_platform_404(client):
    """未知 platform → 404（trae/qoder 未接入前路径可先探）。"""
    assert client.get("/v1/platforms/trae/accounts", headers=_auth()).status_code == 404
    assert client.post("/v1/platforms/qoder/login/start", headers=_auth()).status_code == 404


def test_endpoints_require_api_key(client):
    """全部端点 401（无 Bearer）。"""
    assert client.get("/v1/platforms").status_code == 401
    assert client.get("/v1/platforms/workbuddy/accounts").status_code == 401
    assert client.post("/v1/platforms/workbuddy/login/start").status_code == 401


# ─────────────────────────── 账号管理 ───────────────────────────

async def test_accounts_never_leak_tokens(client):
    """账号列表不含任何 token（响应序列化后断言 access_token/refresh_token 子串）。"""
    await store.upsert_account("workbuddy", {
        "uid": "u_sec", "nickname": "安全号", "access_token": "ACCESS_SECRET_X",
        "refresh_token": "REFRESH_SECRET_X",
        # last_checkin_date 用动态「今天」：昨日日期会被读取路径按日期失效（设计行为）
        "checkin": {"last_checkin_date": store.today_cn(), "streak_days": 1,
                    "today_checked_in": True},
        "quota": {"total": 100, "remaining": 50, "used": 50},
    })
    r = client.get("/v1/platforms/workbuddy/accounts", headers=_auth())
    assert r.status_code == 200
    assert "ACCESS_SECRET_X" not in r.text
    assert "REFRESH_SECRET_X" not in r.text
    accounts = r.json()["accounts"]
    assert accounts[0]["uid"] == "u_sec"
    assert accounts[0]["nickname"] == "安全号"
    assert accounts[0]["checkin"]["today_checked_in"] is True


async def test_delete_account(client):
    await store.upsert_account("workbuddy", {"uid": "u_del", "access_token": "at"})
    assert client.delete("/v1/platforms/workbuddy/accounts/u_del",
                         headers=_auth()).status_code == 200
    assert client.delete("/v1/platforms/workbuddy/accounts/u_del",
                         headers=_auth()).status_code == 404


# ─────────────────────────── 登录流 ───────────────────────────

def test_login_start_and_status_via_routes(client):
    """login/start → pending → 轮询 status（MockTransport 全序列，经 HTTP 端点）。"""
    _mock_wb({
        f"{API}/v2/plugin/auth/state": httpx.Response(
            200, json={"code": 0, "data": {"state": "st_rt", "authUrl": "https://x/login"}}),
        f"{API}/v2/plugin/auth/token": httpx.Response(
            200, json={"code": 0, "data": {"accessToken": "at", "expiresIn": 60}}),
        f"{API}/v2/plugin/login/account": httpx.Response(
            200, json={"code": 0, "data": {"uid": "u_rt", "nickname": "路由号"}}),
    })

    r = client.post("/v1/platforms/workbuddy/login/start", headers=_auth())
    assert r.status_code == 200
    started = r.json()
    assert started["login_id"].startswith("wb_")
    assert started["expires_in"] == 600

    # 轮询到成功（对齐前端轮询；TestClient 同步循环 + 槽 task 由 relay 事件循环推进）
    status = {"status": "pending"}
    for _ in range(50):
        rr = client.get("/v1/platforms/workbuddy/login/status",
                        params={"login_id": started["login_id"]}, headers=_auth())
        status = rr.json()
        if status["status"] != "pending":
            break
        client.get("/v1/platforms", headers=_auth())  # 让事件循环跑其他 task
    assert status["status"] == "success"


def test_login_cancel(client):
    _mock_wb({
        f"{API}/v2/plugin/auth/state": httpx.Response(
            200, json={"code": 0, "data": {"state": "st_cc", "authUrl": "u"}}),
        f"{API}/v2/plugin/auth/token": httpx.Response(200, json={"code": 1, "msg": "w"}),
    })
    started = client.post("/v1/platforms/workbuddy/login/start", headers=_auth()).json()
    r = client.post("/v1/platforms/workbuddy/login/cancel",
                    params={"login_id": started["login_id"]}, headers=_auth())
    assert r.status_code == 200 and r.json()["status"] == "cancelled"


# ─────────────────────────── 签到与配额 ───────────────────────────

async def test_checkin_via_routes(client):
    """checkin：CheckinResult + 更新后 AccountView；token 有效不发 refresh。"""
    await store.upsert_account("workbuddy", {
        "uid": "u_ck", "access_token": "at", "expires_at": 9_999_999_999_999})
    _mock_wb({
        f"{METER}/daily-checkin": httpx.Response(200, json={
            "code": 0, "data": {"streak_days": 2, "credit": 15}}),
    })
    r = client.post("/v1/platforms/workbuddy/accounts/u_ck/checkin", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["result"]["success"] is True
    assert body["result"]["streak_days"] == 2
    assert body["account"]["uid"] == "u_ck"
    assert body["account"]["checkin"]["today_checked_in"] is True
    assert "ACCESS" not in r.text  # token 不回显兜底断言


async def test_checkin_401_maps_login_required(client):
    """上游 401 → HTTP 401 且 detail.code=login_required（前端引导重登）。"""
    await store.upsert_account("workbuddy", {
        "uid": "u_401", "access_token": "at", "expires_at": 9_999_999_999_999})
    _mock_wb({
        f"{METER}/daily-checkin": httpx.Response(401, json={"code": 401, "msg": "expired"}),
    })
    r = client.post("/v1/platforms/workbuddy/accounts/u_401/checkin", headers=_auth())
    assert r.status_code == 401
    assert r.json()["detail"]["code"] == "login_required"


async def test_quota_via_routes(client):
    await store.upsert_account("workbuddy", {
        "uid": "u_q", "access_token": "at", "expires_at": 9_999_999_999_999})
    _mock_wb({
        f"{METER}/get-user-resource": httpx.Response(200, json={
            "code": 0, "data": {"resources": [
                {"CapacitySizePrecise": "100", "CapacityUsedPrecise": "30",
                 "DeductionEndTime": "2026-12-31"}]}}),
    })
    r = client.post("/v1/platforms/workbuddy/accounts/u_q/quota", headers=_auth())
    assert r.status_code == 200
    assert r.json()["quota"]["remaining"] == 70


# ─────────────────────────── ta3 回归（挂载不侵入）──────────────────────────

def test_ta3_routes_unaffected(client):
    """platforms 挂载后 ta3 管理路由与 OpenAI 服务开关语义不受影响（仅验证路由匹配）。"""
    # /v1/auth/status 仍走原实现
    r = client.get("/v1/auth/status", headers=_auth())
    assert r.status_code == 200
    # /v1/service 仍可用
    assert client.get("/v1/service", headers=_auth()).status_code == 200
    # /v1/platforms 未挤占 OpenAI 路径
    assert client.get("/v1/models", headers=_auth()).status_code in (200, 503)
