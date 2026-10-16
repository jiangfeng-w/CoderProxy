"""WorkBuddy /v1/platforms/* 管理端点单测：注册表分发、错误映射、安全断言、ta3 回归。

不触网：oauth/billing 模块的 WbClient 以 MockTransport 注入；存储落 tmp。
"""
import json

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


# ─────────────────────────── IDE 账号切换 ───────────────────────────

def test_ide_endpoints_require_api_key(client):
    assert client.get("/v1/platforms/workbuddy/ide-state").status_code == 401
    assert client.post("/v1/platforms/workbuddy/accounts/x/ide-switch").status_code == 401
    assert client.post("/v1/platforms/workbuddy/accounts/import").status_code == 401
    assert client.post("/v1/platforms/workbuddy/accounts/import-local").status_code == 401


def test_ide_state_via_routes(client, monkeypatch):
    from relay.platforms.workbuddy import ide_switch

    async def fake_ide_state(profile=None):
        return {"installed": True, "exe_path": "X:/wb/WorkBuddy.exe", "auth_exists": True,
                "logged_out": False, "current_uid": "u_live", "running": False}

    monkeypatch.setattr(ide_switch, "ide_state", fake_ide_state)
    r = client.get("/v1/platforms/workbuddy/ide-state", headers=_auth())
    assert r.status_code == 200
    assert r.json()["current_uid"] == "u_live"
    assert r.json()["running"] is False


async def test_ide_switch_via_routes(client, monkeypatch):
    """切换端点把账号池凭证（含 token）交给编排层，本机副作用全部 mock。"""
    from relay.platforms.workbuddy import ide_switch

    await store.upsert_account("workbuddy", {"uid": "u_sw", "access_token": "at-sw"})
    calls: dict = {}

    async def fake_switch(account, profile=None):
        calls["account"] = account
        calls["profile"] = profile
        return {"status": "ok", "current_uid": account["uid"]}

    monkeypatch.setattr(ide_switch, "switch_account", fake_switch)
    r = client.post("/v1/platforms/workbuddy/accounts/u_sw/ide-switch", headers=_auth())
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "current_uid": "u_sw"}
    assert calls["account"]["access_token"] == "at-sw"


async def test_ide_switch_404_unknown_account(client):
    r = client.post("/v1/platforms/workbuddy/accounts/u_missing/ide-switch", headers=_auth())
    assert r.status_code == 404


async def test_ide_switch_error_maps_409(client, monkeypatch):
    """IdeSwitchError（客户端缺失/密钥/格式/hash 冲突）→ 409 + 中文 detail。"""
    from relay.platforms.workbuddy import ide_switch

    await store.upsert_account("workbuddy", {"uid": "u_err", "access_token": "at"})

    async def failing(account, profile=None):
        raise ide_switch.IdeSwitchError("WorkBuddy 官方加密格式不受支持，已停止覆盖")

    monkeypatch.setattr(ide_switch, "switch_account", failing)
    r = client.post("/v1/platforms/workbuddy/accounts/u_err/ide-switch", headers=_auth())
    assert r.status_code == 409
    assert "不受支持" in r.json()["detail"]


# ─────────────────────────── 账号导入（粘贴/JSON/本机） ───────────────────────────

async def test_import_json_via_routes(client):
    """JSON 粘贴导入：不联网、批量、响应经 AccountView 不含 token。"""
    text = json.dumps([
        {"access_token": "ACCESS_SECRET_1", "uid": "u_imp1", "nickname": "导入一号"},
        {"access_token": "ACCESS_SECRET_2"},
    ])
    r = client.post("/v1/platforms/workbuddy/accounts/import",
                    json={"text": text}, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["imported"] == 2
    assert body["accounts"][0]["uid"] == "u_imp1"
    assert body["accounts"][1]["uid"].startswith("wb_")  # 无 uid 降级主键
    assert "ACCESS_SECRET" not in r.text  # 安全断言：响应不含 token


async def test_import_plain_token_via_routes(client, monkeypatch):
    """纯 token 走 accounts 补全（MockTransport）；预取 mock 掉。"""
    from relay.platforms.workbuddy import imports as wb_imports

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/v2/plugin/accounts")
        return httpx.Response(200, json={"code": 0, "data": {"accounts": [
            {"uid": "u_tok", "nickname": "贴入号", "lastLogin": True}]}})

    client_ctx = _ClientCtx(oauth.WbClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(wb_imports, "WbClient", lambda: client_ctx)

    async def fake_prefetch(account):
        return

    monkeypatch.setattr(oauth, "prefetch_caches", fake_prefetch)
    r = client.post("/v1/platforms/workbuddy/accounts/import",
                    json={"text": "u_tok+jwt-token"}, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["imported"] == 1
    assert body["accounts"][0]["uid"] == "u_tok"
    assert "jwt-token" not in r.text


def test_import_parse_error_maps_400(client):
    r = client.post("/v1/platforms/workbuddy/accounts/import",
                    json={"text": "{bad"}, headers=_auth())
    assert r.status_code == 400
    assert "不是有效 JSON" in r.json()["detail"]


def test_import_empty_text_maps_400(client):
    r = client.post("/v1/platforms/workbuddy/accounts/import",
                    json={"text": ""}, headers=_auth())
    assert r.status_code == 400


def test_import_local_maps_409_when_absent(client, monkeypatch):
    """本机无登录态 → 409（中文 detail）。"""
    from relay.platforms.workbuddy import ide_switch
    from relay.platforms.workbuddy import imports as wb_imports

    monkeypatch.setattr(ide_switch, "auth_file_path", lambda profile=None: None)
    r = client.post("/v1/platforms/workbuddy/accounts/import-local", headers=_auth())
    assert r.status_code == 409
    assert "未在本机" in r.json()["detail"]
    assert wb_imports.PLATFORM_ID == "workbuddy"


# ─────────────── 双域端点细分（粘贴 import 泛化；CN 专用路径未挂载） ───────────────

def test_platform_import_generalized_dual_realm(client):
    """泛化粘贴导入：workbuddy 与 workbuddy-global 双平台可用（JSON 源不联网）。"""
    r = client.post("/v1/platforms/workbuddy-global/accounts/import",
                    json={"text": '[{"access_token": "tok_g", "uid": "u_g", '
                                  '"domain": "www.workbuddy.ai"}]'},
                    headers=_auth())
    assert r.status_code == 200
    assert r.json()["accounts"][0]["uid"] == "u_g"
    r2 = client.post("/v1/platforms/workbuddy/accounts/import",
                     json={"text": '[{"access_token": "tok_c", "uid": "u_c", '
                                   '"domain": "www.codebuddy.cn"}]'},
                     headers=_auth())
    assert r2.status_code == 200
    assert r2.json()["accounts"][0]["uid"] == "u_c"


def test_platform_import_unknown_platform_404(client):
    r = client.post("/v1/platforms/nope/accounts/import",
                    json={"text": "tok"}, headers=_auth())
    assert r.status_code == 404


def test_platform_import_ownership_rejected(client):
    """归属校验经路由映射 400（国际号进 CN 泛化入口）。"""
    r = client.post("/v1/platforms/workbuddy/accounts/import",
                    json={"text": '[{"access_token": "t", "uid": "u", '
                                  '"domain": "www.workbuddy.ai"}]'},
                    headers=_auth())
    assert r.status_code == 400
    assert "国际版" in r.json()["detail"]


def test_import_local_export_mounted_for_global(client, monkeypatch):
    """import-local / export / ide-state / ide-switch 对 workbuddy-global 已挂载（二期）。

    2026-10-15 二期：国际版客户端集成端点全部泛化为 /{platform}/...——
    本机无国际版登录态/账号时业务错误映射照旧（409/404），未知平台仍 404。
    """
    from relay.platforms.workbuddy import ide_switch, imports as wb_imports, exports

    # ide-state：国际版 profile 分派到 orchestration（mock 掉，验路由通）
    async def fake_ide_state(profile=None):
        return {"installed": True, "exe_path": "", "auth_exists": False,
                "logged_out": False, "current_uid": "", "running": False,
                "platform": profile.platform_id if profile else ""}

    monkeypatch.setattr(ide_switch, "ide_state", fake_ide_state)
    r0 = client.get("/v1/platforms/workbuddy-global/ide-state", headers=_auth())
    assert r0.status_code == 200
    assert r0.json()["platform"] == "workbuddy-global"

    # import-local：无登录态 → 409（业务映射，非 404/405）
    async def fake_import_local(profile=None):
        raise wb_imports.ImportParseError("未在本机 WorkBuddy 客户端中找到登录信息")

    monkeypatch.setattr(wb_imports, "import_local", fake_import_local)
    r1 = client.post("/v1/platforms/workbuddy-global/accounts/import-local",
                     headers=_auth())
    assert r1.status_code == 409

    # export：空池 → 400（业务映射，非 404/405）；格式错误 → 400
    r2 = client.post("/v1/platforms/workbuddy-global/accounts/export",
                     json={"uids": [], "format": "cockpit"}, headers=_auth())
    assert r2.status_code == 400
    assert "没有可导出" in r2.json()["detail"]

    # ide-switch：未知账号 → 404
    r3 = client.post("/v1/platforms/workbuddy-global/accounts/u_x/ide-switch",
                     headers=_auth())
    assert r3.status_code == 404

    # 未知平台：仍 404
    r4 = client.get("/v1/platforms/nope/ide-state", headers=_auth())
    assert r4.status_code == 404


# ─────────────────────────── 账号导出（cockpit / 9router） ───────────────────────────

async def test_export_cockpit_via_routes(client):
    """cockpit 格式导出：含 token 属导出语义；字段对齐实物（id=md5(uid)）。"""
    await store.upsert_account("workbuddy", {
        "uid": "u_exp", "nickname": "导出号", "access_token": "ACCESS_SECRET_X",
        "refresh_token": "REFRESH_SECRET_X", "expires_at": 1794144709133})
    r = client.post("/v1/platforms/workbuddy/accounts/export",
                    json={"uids": ["u_exp"], "format": "cockpit"}, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "cockpit"
    assert body["filename"].startswith("workbuddy_accounts_")
    doc = json.loads(body["content"])
    assert doc[0]["uid"] == "u_exp"
    assert doc[0]["access_token"] == "ACCESS_SECRET_X"  # 导出语义：含 token（本地使用）
    assert doc[0]["id"].startswith("workbuddy_")


async def test_export_9router_fragment_when_no_db(client, monkeypatch, tmp_path):
    from relay.platforms.workbuddy import exports as wb_exports

    monkeypatch.setenv("RELAY_9ROUTER_DB", str(tmp_path / "nope.sqlite"))
    await store.upsert_account("workbuddy", {"uid": "u_frag", "access_token": "tok-frag"})
    r = client.post("/v1/platforms/workbuddy/accounts/export",
                    json={"uids": [], "format": "9router"}, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "fragment"
    [conn] = json.loads(body["content"])
    assert conn["provider"] == "codebuddy-cn"
    assert wb_exports.NINE_ROUTER_PROVIDER == "codebuddy-cn"


async def test_export_9router_full_backup_via_routes(client, monkeypatch, tmp_path):
    from relay.platforms.workbuddy import exports as wb_exports
    from tests.test_wb_exports import _make_9router_db, _conn_row

    db = tmp_path / "data.sqlite"
    _make_9router_db(db, connections=[_conn_row("c-1", token="plain")])
    monkeypatch.setenv("RELAY_9ROUTER_DB", str(db))
    await store.upsert_account("workbuddy", {"uid": "u_full", "access_token": "tok-full"})
    r = client.post("/v1/platforms/workbuddy/accounts/export",
                    json={"uids": [], "format": "9router"}, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "full_backup"
    doc = json.loads(body["content"])
    assert "settings" in doc and "apiKeys" in doc  # 完整备份承载全部段
    assert wb_exports.PLATFORM_ID == "workbuddy"


def test_export_errors_via_routes(client):
    # 格式非法 → 400；未知 uid → 404（空池也先走格式校验）
    r = client.post("/v1/platforms/workbuddy/accounts/export",
                    json={"uids": [], "format": "sub2api"}, headers=_auth())
    assert r.status_code == 400
    r = client.post("/v1/platforms/workbuddy/accounts/export",
                    json={"uids": ["nope"], "format": "cockpit"}, headers=_auth())
    assert r.status_code in (404, 400)  # 空池 →「没有可导出」；非空池 → 404 未知 uid
    assert client.post("/v1/platforms/workbuddy/accounts/export").status_code == 401


class _ClientCtx:
    """路由测试用 WbClient 薄包装（对齐 test_wb_imports._Ctx）。"""

    def __init__(self, client: "oauth.WbClient"):
        self._client = client

    async def request_json(self, *args, **kwargs):
        return await self._client.request_json(*args, **kwargs)

    async def close(self):
        await self._client.close()


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


# ─────────── 三需求同批（2026-10-17）：端点 / 字段透出 / 错误映射 / 门控 ───────────


async def test_set_priority_via_routes(client):
    """priority 端点：200 落盘 / 404 / 400（bool 与越界拒绝）。"""
    await store.upsert_account("workbuddy", {"uid": "u_p", "access_token": "at"})
    r = client.post("/v1/platforms/workbuddy/accounts/u_p/priority",
                    headers=_auth(), json={"priority": 7})
    assert r.status_code == 200
    assert r.json()["priority"] == 7
    acc = await store.find_account("workbuddy", "u_p")
    assert acc["priority"] == 7
    assert client.post("/v1/platforms/workbuddy/accounts/u_missing/priority",
                       headers=_auth(), json={"priority": 1}).status_code == 404
    for bad in (0, 100, "1", True, None):
        r = client.post("/v1/platforms/workbuddy/accounts/u_p/priority",
                        headers=_auth(), json={"priority": bad})
        assert r.status_code == 400


async def test_set_enabled_via_routes(client):
    """enabled 端点：200 落盘（幂等）/ 404 / 400（严格 bool——1/0 拒绝）。"""
    await store.upsert_account("workbuddy", {"uid": "u_e", "access_token": "at"})
    r = client.post("/v1/platforms/workbuddy/accounts/u_e/enabled",
                    headers=_auth(), json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    acc = await store.find_account("workbuddy", "u_e")
    assert acc["enabled"] is False
    r = client.post("/v1/platforms/workbuddy/accounts/u_e/enabled",
                    headers=_auth(), json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] is True
    assert client.post("/v1/platforms/workbuddy/accounts/u_missing/enabled",
                       headers=_auth(), json={"enabled": False}).status_code == 404
    for bad in (1, 0, "false", None):
        r = client.post("/v1/platforms/workbuddy/accounts/u_e/enabled",
                        headers=_auth(), json={"enabled": bad})
        assert r.status_code == 400


async def test_accounts_expose_governance_fields(client):
    """AccountView 透出 priority/enabled/degraded_until/model_cooldowns；不含 token；
    已过期冷却条目惰性清理、内部 hits 不透出。"""
    await store.upsert_account("workbuddy", {
        "uid": "u_gov", "access_token": "AT_SECRET", "refresh_token": "RT_SECRET",
        "priority": 3, "enabled": False,
        "degraded_until": 4_000_000_000_000,
        "model_cooldowns": {
            "glm-5.3": {"until": 4_000_000_000_000, "reason": "6004", "hits": 2},
            "old-model": {"until": 1, "reason": "11102"},
        },
    })
    r = client.get("/v1/platforms/workbuddy/accounts", headers=_auth())
    assert r.status_code == 200
    assert "AT_SECRET" not in r.text and "RT_SECRET" not in r.text
    acc = r.json()["accounts"][0]
    assert acc["priority"] == 3
    assert acc["enabled"] is False
    assert acc["degraded_until"] == 4_000_000_000_000
    cds = acc["model_cooldowns"]
    assert cds["glm-5.3"]["reason"] == "6004"
    assert "hits" not in cds["glm-5.3"]
    assert "old-model" not in cds  # 已过期条目惰性清理


def test_upstream_error_mappings_governance():
    """AllAccountsSuspendedError → 429 + Retry-After；ModelUnavailableError →
    400 + code=model_unavailable（spec §4 / §8-A）。"""
    from relay.platforms.workbuddy.chat import (
        AllAccountsSuspendedError,
        ModelUnavailableError,
    )
    from relay.routes import _upstream_error_response

    resp = _upstream_error_response(AllAccountsSuspendedError("全池挂起", retry_after_s=42))
    assert resp.status_code == 429
    assert resp.headers["Retry-After"] == "42"

    resp = _upstream_error_response(ModelUnavailableError("模型全锁", accounts=3,
                                                          unlock_at_ms=123))
    assert resp.status_code == 400
    body = json.loads(resp.body)
    assert body["error"]["code"] == "model_unavailable"


async def test_availability_respects_disabled(client):
    """门控：全停用 ≈ 池空（provider_available False / _any_platform_available 跳过该池；
    双池另一池正常时服务仍可用）。"""
    from relay import adapter_registry
    from relay.routes import _any_platform_available

    await store.upsert_account("workbuddy", {"uid": "u_a", "access_token": "at"})
    assert await adapter_registry.provider_available("wbcn") is True
    assert await _any_platform_available() is True
    await store.update_account("workbuddy", "u_a", {"enabled": False})
    assert await adapter_registry.provider_available("wbcn") is False
    assert await _any_platform_available() is False
    await store.upsert_account("workbuddy-global", {"uid": "u_g", "access_token": "at"})
    assert await _any_platform_available() is True
    await store.update_account("workbuddy-global", "u_g", {"enabled": False})
    assert await _any_platform_available() is False


async def test_governance_endpoints_generalized_global_realm(client):
    """泛化端点双域：国际版路径的 enabled/priority 同样生效。"""
    await store.upsert_account("workbuddy-global",
                               {"uid": "u_g", "access_token": "at"})
    r = client.post("/v1/platforms/workbuddy-global/accounts/u_g/enabled",
                    headers=_auth(), json={"enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    r = client.post("/v1/platforms/workbuddy-global/accounts/u_g/priority",
                    headers=_auth(), json={"priority": 3})
    assert r.status_code == 200 and r.json()["priority"] == 3
    acc = await store.find_account("workbuddy-global", "u_g")
    assert acc["enabled"] is False and acc["priority"] == 3
