"""M4 relay 侧适配 keyless 单测：模型白名单、监控、配置端点、CLI 端口分配。

不触发牛码网络请求：
- storage 数据落 tmp 目录（monkeypatch settings.data_dir）；
- _ensure_model 的目录同步用空实现顶替；
- CLI _pick_port 仅本机 socket 探测。
"""
import asyncio
import socket

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from relay import cli, monitor as monitor_mod, storage, tool_disguise
from relay.config import settings
from relay.routes import app as relay_app


def _run(coro):
    return asyncio.run(coro)


def _tool(name: str, parameters: dict | None = None) -> dict:
    return {"type": "function",
            "function": {"name": name, "parameters": parameters or {"type": "object"}}}


# ─────────────────────────── storage：白名单 / api_key ───────────────────────────

def test_model_whitelist_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    saved = _run(storage.set_model_whitelist(["glm-x", "kimi-y"]))
    assert saved == ["glm-x", "kimi-y"]
    assert _run(storage.get_model_whitelist()) == ["glm-x", "kimi-y"]
    assert _run(storage.is_model_enabled("glm-x")) is True
    assert _run(storage.is_model_enabled("other")) is False


def test_empty_whitelist_all_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(storage.set_model_whitelist([]))
    assert _run(storage.get_model_whitelist()) == []
    assert _run(storage.is_model_enabled("anything")) is True


def test_save_api_key_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "relay_api_key", "")
    _run(storage.save_api_key("m4-key"))
    assert settings.relay_api_key == "m4-key"
    # settings 置空后仍从落盘读到（等同新进程）
    settings.relay_api_key = ""
    assert storage.relay_api_key() == "m4-key"


# ─────────────────────────── monitor：事件缓冲 / 统计 ───────────────────────────

def test_monitor_emit_stats_events():
    m = monitor_mod.Monitor(capacity=50)
    m.emit("chat_request", model="m1")
    m.emit("chat_request", model="m2")
    m.emit("tool_disguise", map_hits=1)
    assert m.stats()["chat_request"] == 2
    assert m.stats()["tool_disguise"] == 1
    assert [e["id"] for e in m.events(after_id=0)["events"]] == [1, 2, 3]
    # 增量拉取：只返回 after_id 之后
    assert [e["id"] for e in m.events(after_id=2)["events"]] == [3]


def test_monitor_ring_buffer_capacity():
    m = monitor_mod.Monitor(capacity=3)
    for i in range(5):
        m.emit("chat_request", model=f"m{i}")
    assert m.stats()["chat_request"] == 5  # 计数不丢
    assert [e["id"] for e in m.events(after_id=0)["events"]] == [3, 4, 5]


def test_monitor_events_limit():
    m = monitor_mod.Monitor(capacity=50)
    for i in range(10):
        m.emit("chat_request", model=f"m{i}")
    assert [e["id"] for e in m.events(limit=3)["events"]] == [8, 9, 10]


# ─────────────────────────── cli：端口分配 ───────────────────────────

def test_pick_port_zero_random_free():
    port = cli._pick_port("127.0.0.1", 0)
    assert 0 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_pick_port_conflict_switches():
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    busy = blocker.getsockname()[1]
    try:
        free = cli._pick_port("127.0.0.1", busy)
    finally:
        blocker.close()
    assert free != busy
    assert free > busy
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", free))


def test_pick_port_free_keeps_same():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert cli._pick_port("127.0.0.1", port) == port


# ─────────────────────────── 工具伪装计数（M4 监控字段）───────────────────────────

def test_disguise_context_counters():
    tools = [_tool("fs_read"), _tool("apply_patch"), _tool("glob")]
    ctx = tool_disguise.build_disguise_context(tools, "hybrid")
    assert ctx.tool_map_hits == 1
    assert ctx.tool_longtail_passthrough == 2
    assert ctx.tool_dropped == 0
    ctx = tool_disguise.build_disguise_context(tools, "strict")
    assert ctx.tool_map_hits == 1
    assert ctx.tool_dropped == 2
    ctx = tool_disguise.build_disguise_context(tools, "passthrough")
    assert ctx.tool_longtail_passthrough == 3


# ─────────────────────────── routes：HTTP 端点（TestClient）───────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tool_mode", "hybrid")
    settings.relay_api_key = "m4-test-key"
    with TestClient(relay_app) as c:
        yield c
    settings.relay_api_key = ""


def _auth():
    return {"Authorization": "Bearer m4-test-key"}


def test_models_endpoint_whitelist_filter(client):
    _run(storage.save_models([
        {"name": "glm-x", "api_key": "k", "base_url": "b", "anthropic": False},
        {"name": "kimi-y", "api_key": "k", "base_url": "b", "anthropic": True},
    ]))
    names = [m["id"] for m in client.get("/v1/models", headers=_auth()).json()["data"]]
    assert set(names) == {"glm-x", "kimi-y"}
    # 白名单子集 → 只暴露勾选模型
    _run(storage.set_model_whitelist(["glm-x"]))
    names = [m["id"] for m in client.get("/v1/models", headers=_auth()).json()["data"]]
    assert names == ["glm-x"]


def test_models_endpoint_requires_auth(client):
    assert client.get("/v1/models").status_code == 401


def test_config_get_and_post(client):
    r = client.get("/v1/auth/config", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["api_key"] == "m4-test-key"
    assert body["tool_mode"] == "hybrid"
    assert body["model_whitelist"] == []
    assert body["port"] == settings.relay_port

    r = client.post("/v1/auth/config", json={
        "api_key": "new-key",
        "tool_mode": "strict",
        "model_whitelist": ["glm-x"],
    }, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["api_key"] == "new-key"
    assert body["tool_mode"] == "strict"
    assert body["model_whitelist"] == ["glm-x"]
    assert settings.tool_mode == "strict"
    # 落盘持久化（GUI 重启后仍生效）
    assert _run(storage.get_model_whitelist()) == ["glm-x"]


def test_config_post_requires_auth(client):
    assert client.post("/v1/auth/config", json={"tool_mode": "strict"}).status_code == 401


def test_config_post_invalid(client):
    assert client.post("/v1/auth/config", json={"tool_mode": "bogus"}, headers=_auth()).status_code == 400
    assert client.post("/v1/auth/config", json={"model_whitelist": "nope"}, headers=_auth()).status_code == 400
    assert client.post("/v1/auth/config", json={"api_key": "  "}, headers=_auth()).status_code == 400


def test_monitor_endpoints_auth_and_incremental(client):
    assert client.get("/v1/monitor/stats").status_code == 401
    assert client.get("/v1/monitor/events").status_code == 401
    assert client.get("/v1/monitor/stats", headers=_auth()).status_code == 200
    body = client.get("/v1/monitor/events", headers=_auth()).json()
    assert "events" in body and "stats" in body

    # 直接 emit 后，增量（after_id）可拉到该事件
    before = client.get("/v1/monitor/events", headers=_auth()).json()
    last_id = max((e["id"] for e in before["events"]), default=0)
    monitor_mod.monitor.emit("chat_request", model="t")
    after = client.get("/v1/monitor/events", headers=_auth(),
                       params={"after_id": last_id}).json()
    assert [e["kind"] for e in after["events"]] == ["chat_request"]


def test_ensure_model_whitelist(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    import relay.routes as routes

    async def _no_sync():
        return []

    monkeypatch.setattr(routes.auth_flow, "sync_models", _no_sync)
    _run(storage.save_models([
        {"name": "glm-x", "api_key": "k", "base_url": "b", "anthropic": False},
        {"name": "kimi-y", "api_key": "k", "base_url": "b", "anthropic": True},
    ]))
    _run(storage.set_model_whitelist(["glm-x"]))
    assert _run(routes._ensure_model("glm-x"))["name"] == "glm-x"
    with pytest.raises(HTTPException) as ei:
        _run(routes._ensure_model("kimi-y"))
    assert ei.value.status_code == 404
