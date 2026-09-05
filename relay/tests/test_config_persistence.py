"""配置持久化（D1）与启动装载 keyless 单测：tool_mode 落盘 / 写路径 / 装载优先级。

不触发牛码网络请求：数据落 tmp 目录（monkeypatch settings.data_dir）；
TestClient 场景沿用 test_m4 的「已登录 + 服务开」基准。
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from relay import routes as routes_mod, storage
from relay.config import settings
from relay.routes import app as relay_app


def _run(coro):
    return asyncio.run(coro)


# ─────────────────────────── storage：tool_mode 持久化 ───────────────────────────

def test_tool_mode_default(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    assert _run(storage.get_tool_mode()) == storage.DEFAULT_TOOL_MODE


def test_tool_mode_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(storage.save_tool_mode("strict"))
    assert _run(storage.get_tool_mode()) == "strict"
    # settings 置回默认后仍从落盘读到（等同新进程重启）
    assert _run(storage.get_tool_mode()) == "strict"


def test_tool_mode_invalid_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    # 直接写非法值后，get_tool_mode 应回退默认（防御损坏状态）
    import json

    p = storage.state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"config": {"tool_mode": "bogus"}}), "utf-8")
    assert _run(storage.get_tool_mode()) == storage.DEFAULT_TOOL_MODE


# ─────────────────────────── D1 启动装载：持久化优先 ───────────────────────────

def test_apply_persisted_config_disk_wins(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(storage.save_tool_mode("passthrough"))
    monkeypatch.setattr(settings, "tool_mode", "hybrid")  # 模拟 env/默认兜底值
    _run(storage.apply_persisted_config())
    assert settings.tool_mode == "passthrough"  # 磁盘值覆盖 env 兜底


def test_apply_persisted_config_env_fallback_when_disk_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tool_mode", "strict")  # 模拟 TOOL_MODE env 兜底
    _run(storage.apply_persisted_config())
    assert settings.tool_mode == "strict"  # 磁盘无值 → 不覆写 env 兜底


def test_apply_persisted_config_invalid_disk_keeps_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tool_mode", "hybrid")
    import json

    p = storage.state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"config": {"tool_mode": "bogus"}}), "utf-8")
    _run(storage.apply_persisted_config())
    assert settings.tool_mode == "hybrid"  # 损坏值不装载，保持 env/默认


# ─────────────────────────── ensure_initialized：api_key 磁盘优先 ───────────────────────────

def test_ensure_api_key_disk_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(storage.save_api_key("disk-key"))
    monkeypatch.setattr(settings, "relay_api_key", "env-key")  # 模拟 RELAY_API_KEY env
    _run(storage.ensure_initialized())
    assert settings.relay_api_key == "disk-key"


def test_ensure_api_key_env_seed_when_disk_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "relay_api_key", "env-key")
    _run(storage.ensure_initialized())
    assert settings.relay_api_key == "env-key"
    # env 兜底不落盘（磁盘文件不产生，后续无 env 仍会再生成）
    assert not storage.state_path().exists()


# ─────────────────────────── routes：写路径落盘 + 全字段校验 ───────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "tool_mode", "hybrid")
    settings.relay_api_key = "cfg-key"

    async def _serving_on():
        return True

    monkeypatch.setattr(routes_mod, "_serving", _serving_on)

    with TestClient(relay_app) as c:
        yield c
    settings.relay_api_key = ""


def _auth():
    return {"Authorization": "Bearer cfg-key"}


def test_config_post_tool_mode_persists(client):
    r = client.post("/v1/auth/config", json={"tool_mode": "strict"}, headers=_auth())
    assert r.status_code == 200
    assert r.json()["tool_mode"] == "strict"
    assert settings.tool_mode == "strict"
    # 落盘：读回（重启后仍生效）
    assert _run(storage.get_tool_mode()) == "strict"


def test_config_post_validate_all_before_write(client):
    # api_key 非法 + tool_mode 合法同体：应 400 且 tool_mode 不被写
    r = client.post("/v1/auth/config",
                    json={"api_key": "  ", "tool_mode": "strict"}, headers=_auth())
    assert r.status_code == 400
    assert settings.tool_mode == "hybrid"
    assert _run(storage.get_tool_mode()) == storage.DEFAULT_TOOL_MODE


def test_config_post_tool_mode_invalid_keeps_disk(client):
    client.post("/v1/auth/config", json={"tool_mode": "passthrough"}, headers=_auth())
    assert client.post("/v1/auth/config", json={"tool_mode": "bogus"},
                       headers=_auth()).status_code == 400
    assert settings.tool_mode == "passthrough"  # 非法请求不破坏运行值
    assert _run(storage.get_tool_mode()) == "passthrough"
