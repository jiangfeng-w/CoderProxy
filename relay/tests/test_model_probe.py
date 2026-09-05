"""连通性测试白名单豁免（probe）keyless 单测。

覆盖需求 docs/spec/连通性测试-白名单豁免（方案 A / body `probe: true`）：
- _ensure_model(probe=True) 跳过白名单启用判定，仅要求模型存在于目录；
- 模型不存在时 probe 同样 404（同步也拉不到）；
- 无 probe（普通 agent 请求）白名单外仍 404（M4 口径不回退）；
- HTTP 层 probe 请求真实走到 provider（上游转发语义），probe 标记不泄漏进请求体；
- probe 与非 probe 走同一 build_provider 构造（伪装同构，无独立裸路径）。

不触发牛码网络请求：storage 落 tmp（monkeypatch settings.data_dir）、
_serving 恒真、build_provider 顶替为记录型替身。
"""
import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.models.schemas import ChatResponse, Usage
from relay import routes as routes_mod, storage
from relay.config import settings
from relay.routes import app as relay_app


def _run(coro):
    return asyncio.run(coro)


_MODELS = [
    {"name": "enabled-model", "api_key": "k", "base_url": "b", "anthropic": False},
    {"name": "other-model", "api_key": "k", "base_url": "b", "anthropic": False},
]


def _auth():
    return {"Authorization": "Bearer probe-test-key"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    settings.relay_api_key = "probe-test-key"

    async def _serving_on():
        return True

    monkeypatch.setattr(routes_mod, "_serving", _serving_on)
    with TestClient(relay_app) as c:
        yield c
    settings.relay_api_key = ""


class _FakeProvider:
    """替身：记录收到的 ChatRequest 并回成功 ChatResponse（不发上游）。"""

    _model_name = "fake"

    def __init__(self):
        self.calls: list = []

    async def chat(self, request):
        self.calls.append(request)
        return ChatResponse(content="ok", finish_reason="stop",
                            usage=Usage(prompt_tokens=2, completion_tokens=1,
                                        total_tokens=3),
                            model=request.model)


# ─────────────────────────── _ensure_model：probe 白名单语义 ───────────────────────────

def test_ensure_model_probe_bypasses_whitelist(tmp_path, monkeypatch):
    """probe=True：白名单外模型放行（仅要求存在）；默认仍 404（M4 回归）。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    async def _no_sync():
        return []

    monkeypatch.setattr(routes_mod.auth_flow, "sync_models", _no_sync)
    _run(storage.save_models(_MODELS))
    _run(storage.set_model_whitelist(["enabled-model"]))

    assert _run(routes_mod._ensure_model("enabled-model"))["name"] == "enabled-model"
    assert _run(routes_mod._ensure_model("other-model", probe=True))["name"] == "other-model"
    with pytest.raises(HTTPException) as ei:
        _run(routes_mod._ensure_model("other-model"))
    assert ei.value.status_code == 404


def test_ensure_model_probe_missing_model_still_404(tmp_path, monkeypatch):
    """probe 不豁免「模型不存在」：目录没有（同步也拉不到）仍 404。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))

    async def _no_sync():
        return []

    monkeypatch.setattr(routes_mod.auth_flow, "sync_models", _no_sync)
    _run(storage.save_models(_MODELS))
    _run(storage.set_model_whitelist(["enabled-model"]))

    with pytest.raises(HTTPException) as ei:
        _run(routes_mod._ensure_model("ghost", probe=True))
    assert ei.value.status_code == 404


# ─────────────────────────── HTTP：/v1/chat/completions probe 行为 ───────────────────────────

def test_chat_without_probe_still_404_for_non_whitelisted(client):
    """无 probe 标记：白名单外模型普通请求仍 404（agent 约束不回退）。"""
    _run(storage.save_models(_MODELS))
    _run(storage.set_model_whitelist(["enabled-model"]))
    resp = client.post("/v1/chat/completions",
                       json={"model": "other-model",
                             "messages": [{"role": "user", "content": "Hi!"}],
                             "stream": False},
                       headers=_auth())
    assert resp.status_code == 404


def test_chat_probe_reaches_upstream_for_non_whitelisted(client, monkeypatch):
    """probe 请求对白名单外模型真实走到转发层；probe 标记不进入 ChatRequest/上游体。"""
    _run(storage.save_models(_MODELS))
    _run(storage.set_model_whitelist(["enabled-model"]))

    fake = _FakeProvider()

    def _fake_build(model, model_name, ctx=None):
        return fake

    monkeypatch.setattr(routes_mod, "build_provider", _fake_build)
    resp = client.post("/v1/chat/completions",
                       json={"model": "other-model",
                             "messages": [{"role": "user", "content": "Hi!"}],
                             "stream": False,
                             "probe": True},
                       headers=_auth())
    assert resp.status_code == 200
    assert len(fake.calls) == 1 and fake.calls[0].model == "other-model"
    # probe 标记仅在 relay 本地消费，不进 ChatRequest / 上游 body
    body = fake.calls[0].model_dump()
    assert "probe" not in body
    assert body["messages"][0]["content"] == "Hi!"


def test_chat_stream_without_probe_non_whitelisted_is_clean_404(client):
    """流式请求白名单外模型：SSE 建立前即返回标准 404，而不是 200 开流后断流。

    （修复：_ensure_model 提前到 StreamingResponse 之前执行；此前 agent 端只见
    Stream error 断流错误，无法识别「未知或未启用的模型」。）
    """
    _run(storage.save_models(_MODELS))
    _run(storage.set_model_whitelist(["enabled-model"]))
    resp = client.post("/v1/chat/completions",
                       json={"model": "other-model",
                             "messages": [{"role": "user", "content": "Hi!"}],
                             "stream": True},
                       headers=_auth())
    assert resp.status_code == 404
    assert "未知或未启用的模型" in resp.json()["detail"]
    assert resp.headers.get("content-type", "").startswith("application/json")


def test_probe_and_normal_share_provider_construction(client, monkeypatch):
    """probe 与普通请求走同一 build_provider（伪装同构、无独立裸路径）。"""
    _run(storage.save_models(_MODELS))
    _run(storage.set_model_whitelist(["enabled-model"]))
    built: list = []

    def _fake_build(model, model_name, ctx=None):
        built.append((model_name, ctx.mode if ctx else None,
                      list(ctx.outbound_tools) if ctx else None))
        return _FakeProvider()

    monkeypatch.setattr(routes_mod, "build_provider", _fake_build)
    payload = {"model": "enabled-model",
               "messages": [{"role": "user", "content": "Hi!"}],
               "stream": False}
    assert client.post("/v1/chat/completions", json=payload,
                       headers=_auth()).status_code == 200
    assert client.post("/v1/chat/completions", json={**payload, "probe": True},
                       headers=_auth()).status_code == 200
    assert built[0] == built[1]

