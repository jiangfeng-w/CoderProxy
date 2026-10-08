"""自定义供应商转发生效单测（§4.4）：传输/白名单重建/错误透传/非流式回退，不触网。"""
import asyncio
import json

import httpx
import pytest

from app.models.schemas import ChatMessage, ChatRequest
from relay import custom_openai
from relay.config import settings
from relay.custom_openai import OpenAICompatProvider, UpstreamHttpError

ENTRY = {"id": "p1", "name": "基元律动", "base_url": "https://api.example.com/v1",
         "api_key": "sk-test", "enabled": True, "models": ["glm-5.3-flash"]}


def _run(coro):
    return asyncio.run(coro)


def _sse(lines):
    return httpx.Response(200, content="".join(f"{ln}\n\n" for ln in lines).encode(),
                          headers={"Content-Type": "text/event-stream"})


_SSE_OK = [
    'data: {"id":"c1","object":"chat.completion.chunk","model":"m","choices":[{"index":0,'
    '"delta":{"role":"assistant","content":"","reasoning_content":"想"},"finish_reason":""}]}',
    'data: {"id":"c1","object":"chat.completion.chunk","model":"m","choices":[{"index":0,'
    '"delta":{"content":"你好","vendor_field":"internal"},"finish_reason":""}]}',
    'data: {"id":"c1","object":"chat.completion.chunk","model":"m","choices":[{"index":0,'
    '"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":5,"completion_tokens":3,'
    '"total_tokens":8,"completion_tokens_details":{"reasoning_tokens":2}}}',
    "data: [DONE]",
]

REQ = ChatRequest(model="glm-5.3-flash",
                  messages=[ChatMessage(role="user", content="hi")])


def _provider(handler):
    return OpenAICompatProvider(ENTRY, "glm-5.3-flash",
                                transport=httpx.MockTransport(handler))


async def _collect(adapter, request):
    return [e async for e in adapter.stream_structured(request)]


# ─────────────────────────── URL / 头 / body ───────────────────────────

def test_url_join_variants():
    assert _provider(lambda r: _sse(["data: [DONE]"]))._url() == \
        "https://api.example.com/v1/chat/completions"
    # 尾斜杠归一（不产生 //）
    p = OpenAICompatProvider({**ENTRY, "base_url": "https://x.com/v1/"}, "m")
    assert p._url() == "https://x.com/v1/chat/completions"
    # 用户连完整端点 → 不二次拼接
    p = OpenAICompatProvider({**ENTRY, "base_url": "https://x.com/v1/chat/completions"}, "m")
    assert p._url() == "https://x.com/v1/chat/completions"


def test_headers_bearer_key():
    h = _provider(lambda r: _sse(["data: [DONE]"]))._headers()
    assert h["Authorization"] == "Bearer sk-test"
    assert h["Content-Type"] == "application/json"


def test_body_shape():
    a = _provider(lambda r: _sse(["data: [DONE]"]))
    body = a._body(REQ, stream=False)
    assert body["model"] == "glm-5.3-flash"
    assert body["stream"] is False
    assert body["messages"][0]["content"] == "hi"


# ─────────────────────────── 流式 ───────────────────────────

def test_stream_events_and_whitelist():
    """白名单重建：vendor_field 等扩展字段不得出现在事件中。"""
    a = _provider(lambda r: _sse(_SSE_OK))
    events = _run(_collect(a, REQ))
    assert events[0] == {"type": "thinking", "delta": "想"}
    assert events[1] == {"type": "content", "delta": "你好"}
    done = events[-1]
    assert done["content"] == "你好" and done["thinking"] == "想"
    assert done["usage"].total_tokens == 8 and done["usage"].reasoning_tokens == 2
    text = json.dumps(events, ensure_ascii=False, default=str)
    assert "vendor_field" not in text


def test_stream_http_error_maps_status():
    a = _provider(lambda r: httpx.Response(429, json={
        "error": {"message": "rate limited"}}))
    with pytest.raises(UpstreamHttpError) as ei:
        _run(_collect(a, REQ))
    assert ei.value.status == 429 and "rate limited" in str(ei.value)


# ─────────────────────────── 非流式 ───────────────────────────

def test_chat_nonstream_passthrough():
    a = _provider(lambda r: httpx.Response(200, json={
        "id": "x", "object": "chat.completion", "model": "glm-5.3-flash",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}))
    resp = _run(a.chat(REQ))
    assert resp.content == "ok" and resp.model == "glm-5.3-flash"
    assert resp.usage.total_tokens == 2


def test_chat_nonstream_falls_back_to_stream():
    """上游 4xx 拒非流式（如强制流式）→ 退回流式聚合。"""
    calls: list[bool] = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(bool(body.get("stream")))
        if not body.get("stream"):
            return httpx.Response(400, json={"error": {"message": "stream required"}})
        return _sse(_SSE_OK)

    a = _provider(handler)
    resp = _run(a.chat(REQ))
    assert calls == [False, True]
    assert resp.content == "你好"


def test_chat_nonstream_error_passthrough():
    a = _provider(lambda r: httpx.Response(401, json={"error": {"message": "bad key"}}))
    with pytest.raises(UpstreamHttpError) as ei:
        _run(a.chat(REQ))
    assert ei.value.status == 401


# ─────────────────────────── 工具调用 ───────────────────────────

def test_stream_tool_call_merge():
    lines = [
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"call_1",'
        '"type":"function","function":{"name":"Bash","arguments":"{\\"cmd"}}]}}]}',
        'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,'
        '"function":{"arguments":"\\":\\"ls\\"}"}}]}}]}',
        'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]
    a = _provider(lambda r: _sse(lines))
    events = _run(_collect(a, REQ))
    done = events[-1]
    assert done["tool_calls"][0]["name"] == "Bash"
    assert done["tool_calls"][0]["arguments"] == {"cmd": "ls"}


# ─────────────────────────── routes 集成（build_adapter 路径） ───────────────────────────

def test_routes_custom_provider_end_to_end(tmp_path, monkeypatch):
    """端到端：配置条目 → /v1/models 合并 → 请求路由 → 假上游收到请求。"""
    from fastapi.testclient import TestClient
    from relay import routes as routes_mod, storage
    from relay.providers_custom import _save_sync

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    settings.relay_api_key = "custom-test-key"
    _save_sync([ENTRY])

    async def _serving_on():
        return True

    monkeypatch.setattr(routes_mod, "_serving", _serving_on)
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "model": "glm-5.3-flash",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "custom-ok"},
                         "finish_reason": "stop"}],
            "usage": {}})

    import relay.custom_openai as co
    orig_init = co.OpenAICompatProvider.__init__

    def _patched_init(self, entry, bare_model, **kw):
        kw.setdefault("transport", httpx.MockTransport(handler))
        orig_init(self, entry, bare_model, **kw)

    monkeypatch.setattr(co.OpenAICompatProvider, "__init__", _patched_init)

    with TestClient(routes_mod.app) as client:
        r = client.get("/v1/models", headers={"Authorization": "Bearer custom-test-key"})
        ids = [m["id"] for m in r.json()["data"]]
        assert "基元律动/glm-5.3-flash" in ids

        # 单模型端点（自定义供应商全名）
        r = client.get("/v1/models/基元律动/glm-5.3-flash",
                       headers={"Authorization": "Bearer custom-test-key"})
        assert r.status_code == 200 and r.json()["id"] == "基元律动/glm-5.3-flash"

        r = client.post("/v1/chat/completions",
                        json={"model": "基元律动/glm-5.3-flash",
                              "messages": [{"role": "user", "content": "hi"}]},
                        headers={"Authorization": "Bearer custom-test-key"})
        assert r.status_code == 200
        body = r.json()
        assert body["choices"][0]["message"]["content"] == "custom-ok"
        assert body["model"] == "基元律动/glm-5.3-flash"
    assert len(seen) == 1
    assert seen[0].headers["authorization"] == "Bearer sk-test"
    assert str(seen[0].url).endswith("/v1/chat/completions")

    settings.relay_api_key = ""


def test_routes_custom_disabled_excluded(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from relay import routes as routes_mod
    from relay.providers_custom import _save_sync

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    settings.relay_api_key = "custom-test-key"
    _save_sync([{**ENTRY, "enabled": False}])

    async def _serving_on():
        return True

    monkeypatch.setattr(routes_mod, "_serving", _serving_on)
    with TestClient(routes_mod.app) as client:
        r = client.get("/v1/models", headers={"Authorization": "Bearer custom-test-key"})
        assert r.json()["data"] == []
        r = client.post("/v1/chat/completions",
                        json={"model": "基元律动/glm-5.3-flash",
                              "messages": [{"role": "user", "content": "hi"}]},
                        headers={"Authorization": "Bearer custom-test-key"})
        assert r.status_code == 404
        assert "未知供应商" in r.json()["detail"]
    settings.relay_api_key = ""


def test_routes_custom_whitelisted_out_message(tmp_path, monkeypatch):
    """白名单未启用该模型：报「未知或未启用的模型」，不误报「未知供应商」。"""
    from fastapi.testclient import TestClient
    from relay import routes as routes_mod, storage
    from relay.providers_custom import _save_sync

    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    settings.relay_api_key = "custom-test-key"
    _save_sync([ENTRY])
    _run(storage.set_model_whitelist(["牛码/glm-5.3-flash"]))

    async def _serving_on():
        return True

    monkeypatch.setattr(routes_mod, "_serving", _serving_on)
    with TestClient(routes_mod.app) as client:
        r = client.post("/v1/chat/completions",
                        json={"model": "基元律动/glm-5.3-flash",
                              "messages": [{"role": "user", "content": "hi"}]},
                        headers={"Authorization": "Bearer custom-test-key"})
        assert r.status_code == 404
        assert "未知或未启用的模型" in r.json()["detail"]
    settings.relay_api_key = ""
