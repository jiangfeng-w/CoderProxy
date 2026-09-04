"""对 agent 的 OpenAI /v1 服务开关（方案B）keyless 单测。

不触发牛码网络请求：
- storage 落 tmp 目录（monkeypatch settings.data_dir）；
- 登录态用 monkeypatch 顶替 routes_mod.auth_flow.login_status，
  聚焦「默认关 → 登录后开 → 手动停 → 重新启 → 登出复位」的门控语义。
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from relay import auth_flow, routes as routes_mod, storage
from relay.config import settings
from relay.routes import app as relay_app


def _run(coro):
    return asyncio.run(coro)


def _auth():
    return {"Authorization": "Bearer gate-test-key"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    settings.relay_api_key = "gate-test-key"
    monkeypatch.setattr(routes_mod, "_service_disabled", False)

    async def _not_logged_in():
        return {"status": "not_logged_in"}

    monkeypatch.setattr(routes_mod.auth_flow, "login_status", _not_logged_in)
    with TestClient(relay_app, raise_server_exceptions=False) as c:
        yield c
    settings.relay_api_key = ""


async def _logged_in():
    return {"status": "logged_in"}


def test_service_default_off_when_not_logged_in(client):
    """未登录：/v1 服务默认关闭（service.enabled=False，chat/models 均 503）。"""
    r = client.get("/v1/service", headers=_auth())
    assert r.status_code == 200 and r.json()["enabled"] is False

    assert client.get("/v1/models", headers=_auth()).status_code == 503
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-x", "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth())
    assert resp.status_code == 503


def test_service_after_login_then_stop_and_reopen(client, monkeypatch):
    """登录后服务自动开；手动停 → 关；重新启 → 开（agent 视角）。"""
    # 登录
    monkeypatch.setattr(routes_mod.auth_flow, "login_status", _logged_in)
    _run(storage.save_models([{"name": "glm-x", "api_key": "k", "base_url": "b",
                               "anthropic": False}]))

    # 登录后：服务开，/v1/models（agent 视角）可访问
    assert client.get("/v1/service", headers=_auth()).json()["enabled"] is True
    m = client.get("/v1/models", headers=_auth()).json()["data"]
    assert [x["id"] for x in m] == ["glm-x"]

    # 手动停止：服务关，agent 入口 503（GUI 用 all=1 不受影响）
    r = client.post("/v1/service/disable", headers=_auth())
    assert r.json()["enabled"] is False
    assert client.get("/v1/service", headers=_auth()).json()["enabled"] is False
    assert client.get("/v1/models", headers=_auth()).status_code == 503
    assert client.get("/v1/models?all=1", headers=_auth()).status_code == 200

    # 重新启用：服务恢复
    r = client.post("/v1/service/enable", headers=_auth())
    assert r.json()["enabled"] is True
    assert client.get("/v1/models", headers=_auth()).status_code == 200


def test_logout_resets_service_disabled(client, monkeypatch):
    """登出复位服务开关：下次登录时对 agent 的服务自动启动。"""
    monkeypatch.setattr(routes_mod.auth_flow, "login_status", _logged_in)
    client.post("/v1/service/disable", headers=_auth())
    assert routes_mod._service_disabled is True

    r = client.post("/v1/auth/logout", headers=_auth())
    assert r.status_code == 200
    assert routes_mod._service_disabled is False


# ─────────────────────────── login_status 归一化（无会话 vs 浏览器授权中）───────────────────────────

async def _stub(status: str, error: str | None = None) -> dict:
    """返回待注入的 fake get_login_status。"""
    d = {"status": status}
    if error is not None:
        d["error"] = error
    return d


async def test_login_status_normalizes_never_logged_in(monkeypatch):
    """无会话：vendored 返回 pending(未登录) → 归一化为 not_logged_in。"""
    async def _never(*_args, **_kwargs):
        return await _stub("pending", "未登录")

    monkeypatch.setattr(auth_flow.ta3_oauth, "get_login_status", _never)
    assert await auth_flow.login_status() == {"status": "not_logged_in"}


async def test_login_status_keeps_inflight_pending(monkeypatch):
    """浏览器授权中：pending（无错误字段）→ 保持 pending。"""
    async def _inflight(*_args, **_kwargs):
        return await _stub("pending")

    monkeypatch.setattr(auth_flow.ta3_oauth, "get_login_status", _inflight)
    assert await auth_flow.login_status() == {"status": "pending"}


async def test_login_status_keeps_logged_in(monkeypatch):
    """已登录：原样返回。"""
    async def _logged(*_args, **_kwargs):
        return await _stub("logged_in", "ok")

    monkeypatch.setattr(auth_flow.ta3_oauth, "get_login_status", _logged)
    st = await auth_flow.login_status()
    assert st == {"status": "logged_in", "error": "ok"}
