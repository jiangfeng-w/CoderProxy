"""自定义供应商配置管理端点（/v1/providers/custom/*）单测。

聚焦供应商页 F5：CRUD 生命周期、api_key 不回显（任何响应不含该子串）、
更新时 api_key 缺省保留旧值、字段校验 400、独立落盘 platform_custom.json。
不触发任何网络请求（storage 落 tmp 目录，monkeypatch settings.data_dir）。
"""
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from relay.config import settings
from relay.routes import app as relay_app

API_KEY_VALUE = "sk-secret-custom-key-123"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    settings.relay_api_key = "gate-test-key"
    with TestClient(relay_app, raise_server_exceptions=False) as c:
        yield c
    settings.relay_api_key = ""


def _auth():
    return {"Authorization": "Bearer gate-test-key"}


def _create(client, **overrides):
    body = {"name": "自建网关", "base_url": "https://api.example.com/v1",
            "api_key": API_KEY_VALUE, "models": ["gpt-x"]}
    body.update(overrides)
    return client.post("/v1/providers/custom", json=body, headers=_auth())


def test_create_list_update_delete_lifecycle(client):
    """CRUD 全生命周期：创建 → 列表 → 更新 → 删除 → 404。"""
    r = _create(client)
    assert r.status_code == 200
    pid = r.json()["provider"]["id"]
    assert pid

    r = client.get("/v1/providers/custom", headers=_auth())
    providers = r.json()["providers"]
    assert len(providers) == 1
    assert providers[0]["name"] == "自建网关"
    assert providers[0]["enabled"] is True
    assert providers[0]["models"] == ["gpt-x"]

    r = client.post(f"/v1/providers/custom/{pid}",
                    json={"name": "改名", "enabled": False}, headers=_auth())
    assert r.status_code == 200
    assert r.json()["provider"]["name"] == "改名"
    assert r.json()["provider"]["enabled"] is False
    # 未出现的字段保留
    assert r.json()["provider"]["base_url"] == "https://api.example.com/v1"

    r = client.delete(f"/v1/providers/custom/{pid}", headers=_auth())
    assert r.status_code == 200
    assert client.get("/v1/providers/custom", headers=_auth()).json()["providers"] == []
    assert client.delete(f"/v1/providers/custom/{pid}", headers=_auth()).status_code == 404
    assert client.post(f"/v1/providers/custom/{pid}", json={"name": "x"},
                       headers=_auth()).status_code == 404


def test_api_key_never_echoed(client):
    """api_key 只落盘不回显：创建/列表/更新响应均不含 key 值，以 has_api_key 表达。"""
    r = _create(client)
    pid = r.json()["provider"]["id"]
    assert r.json()["provider"]["has_api_key"] is True
    assert API_KEY_VALUE not in r.text

    r = client.get("/v1/providers/custom", headers=_auth())
    assert API_KEY_VALUE not in r.text

    # api_key 缺省（键不存在）→ 保留旧值且不回显
    r = client.post(f"/v1/providers/custom/{pid}", json={"name": "改"}, headers=_auth())
    assert API_KEY_VALUE not in r.text
    assert r.json()["provider"]["has_api_key"] is True

    # 落盘文件本身持有 key（本地明文存储，cc-switch 同为先例）
    raw = json.loads(Path(settings.data_dir, "platform_custom.json").read_text("utf-8"))
    assert any(p.get("api_key") == API_KEY_VALUE for p in raw["providers"])

    # 显式传空串 → 清除
    r = client.post(f"/v1/providers/custom/{pid}", json={"api_key": ""}, headers=_auth())
    assert r.json()["provider"]["has_api_key"] is False


def test_update_keeps_api_key_when_absent(client):
    """更新不传 api_key：落盘值不变（下一轮列表 has_api_key 仍为 True）。"""
    pid = _create(client).json()["provider"]["id"]
    client.post(f"/v1/providers/custom/{pid}",
                json={"models": ["a", "b"]}, headers=_auth())
    providers = client.get("/v1/providers/custom", headers=_auth()).json()["providers"]
    assert providers[0]["has_api_key"] is True
    assert providers[0]["models"] == ["a", "b"]


def test_validation_400(client):
    """全字段先校验：name 空 / base_url 非 http(s) / models 非列表 均 400，不落盘。"""
    assert _create(client, name="  ").status_code == 400
    assert _create(client, base_url="api.example.com").status_code == 400
    assert _create(client, models="gpt-x").status_code == 400
    assert _create(client, enabled="yes").status_code == 400
    assert _create(client, api_key=123).status_code == 400
    assert client.get("/v1/providers/custom", headers=_auth()).json()["providers"] == []


def test_independent_state_file(client):
    """独立落盘 platform_custom.json，不写 relay_state.json。"""
    _create(client)
    assert Path(settings.data_dir, "platform_custom.json").exists()
    assert not Path(settings.data_dir, "relay_state.json").exists()


def test_requires_api_key(client):
    """管理端点全部要求 Bearer key（对齐 M6+ 管理端点惯例）。"""
    assert client.get("/v1/providers/custom").status_code == 401
    assert client.post("/v1/providers/custom", json={"name": "x"}).status_code == 401
    assert client.post("/v1/providers/custom/pid", json={}).status_code == 401
    assert client.delete("/v1/providers/custom/pid").status_code == 401
