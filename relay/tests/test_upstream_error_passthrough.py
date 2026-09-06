"""上游错误状态码透传 keyless 单测（不触发牛码网络请求）。

背景：此前上游错误（vendored ta3.py 抛 RuntimeError("模型请求失败 {status}：{body}")）
一律以 HTTP 500 纯文本抛给 agent，ZCode 等客户端把 500 当可重试网络错误做指数退避，
403/402 这类终态错误被盲目重放到放弃。修复后：
- 非流式 / 流式首帧前：透传真实状态码 + OpenAI 风格错误体；
- 流式首帧后（响应头已发）：补一帧 OpenAI 风格错误帧再干净收尾，不再硬断连接。
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from relay import routes as routes_mod, storage
from relay.config import settings
from relay.routes import (
    _extract_upstream_error,
    _is_upstream_401,
    _upstream_error_response,
    app as relay_app,
)


def _run(coro):
    return asyncio.run(coro)


def _auth():
    return {"Authorization": "Bearer upstream-test-key"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    settings.relay_api_key = "upstream-test-key"
    monkeypatch.setattr(routes_mod, "_service_disabled", False)

    async def _logged_in():
        return {"status": "logged_in"}

    monkeypatch.setattr(routes_mod.auth_flow, "login_status", _logged_in)
    _run(storage.save_models([{"name": "glm-x", "api_key": "k", "base_url": "b",
                               "anthropic": False}]))
    with TestClient(relay_app, raise_server_exceptions=False) as c:
        yield c
    settings.relay_api_key = ""


UPSTREAM_403 = '模型请求失败 403：{"error":"Forbidden","message":"请求包含违规内容","status":403}'


# ────────────────── 纯函数：错误解析 ──────────────────

def test_extract_upstream_error_parses_status_and_body():
    exc = RuntimeError(UPSTREAM_403)
    status, text = _extract_upstream_error(exc)
    assert status == 403
    assert json.loads(text)["message"] == "请求包含违规内容"


def test_extract_upstream_error_multiline_body():
    exc = RuntimeError("模型请求失败 429：{\"error\":\n\"rate limited\"}")
    assert _extract_upstream_error(exc)[0] == 429


def test_extract_upstream_error_non_upstream_returns_none():
    assert _extract_upstream_error(ValueError("别的错误")) is None
    assert _extract_upstream_error(RuntimeError("模型请求失败：ConnectionError: x")) is None


def test_is_upstream_401_uses_extractor():
    assert _is_upstream_401(RuntimeError("模型请求失败 401：{\"error\":\"unauthorized\"}"))
    assert not _is_upstream_401(RuntimeError(UPSTREAM_403))


def test_upstream_error_response_passthrough():
    resp = _upstream_error_response(RuntimeError(UPSTREAM_403))
    assert resp.status_code == 403
    payload = json.loads(bytes(resp.body))
    assert payload["error"]["type"] == "upstream_error"
    assert payload["error"]["code"] == 403
    assert "请求包含违规内容" in payload["error"]["message"]


def test_upstream_error_response_non_upstream_returns_none():
    assert _upstream_error_response(ValueError("x")) is None


# ────────────────── 端点：非流式透传 ──────────────────

def test_nonstream_upstream_403_passthrough(client, monkeypatch):
    async def _raise(model_name, chat_request, ctx, *, probe=False):
        raise RuntimeError(UPSTREAM_403)

    monkeypatch.setattr(routes_mod, "_chat_with_retry", _raise)
    r = client.post("/v1/chat/completions",
                    json={"model": "glm-x",
                          "messages": [{"role": "user", "content": "hi"}]},
                    headers=_auth())
    assert r.status_code == 403
    body = r.json()
    assert body["error"]["code"] == 403
    assert "请求包含违规内容" in body["error"]["message"]


def test_nonstream_non_upstream_error_still_500(client, monkeypatch):
    async def _raise(model_name, chat_request, ctx, *, probe=False):
        raise ValueError("解析炸了")

    monkeypatch.setattr(routes_mod, "_chat_with_retry", _raise)
    r = client.post("/v1/chat/completions",
                    json={"model": "glm-x",
                          "messages": [{"role": "user", "content": "hi"}]},
                    headers=_auth())
    assert r.status_code == 500


# ────────────────── 端点：流式 ──────────────────

def test_stream_preflight_upstream_403_passthrough(client, monkeypatch):
    """首帧前上游 403：响应头未发，直接透传 403 而非「200 开流即断」。"""

    async def _raise_iter(model_name, chat_request, include_usage, ctx,
                          usage_collector=None, *, probe=False):
        raise RuntimeError(UPSTREAM_403)
        yield  # pragma: no cover（使其成为 async generator）

    monkeypatch.setattr(routes_mod, "_sse_with_retry", _raise_iter)
    r = client.post("/v1/chat/completions",
                    json={"model": "glm-x", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]},
                    headers=_auth())
    assert r.status_code == 403
    assert r.json()["error"]["code"] == 403


def test_stream_mid_failure_emits_error_frame(client, monkeypatch):
    """首帧后上游失败：200 不变，流尾补 OpenAI 风格错误帧，连接干净收尾。"""

    async def _half_iter(model_name, chat_request, include_usage, ctx,
                         usage_collector=None, *, probe=False):
        yield 'data: {"choices": [{"delta": {"content": "你"}}]}\n\n'
        raise RuntimeError(UPSTREAM_403)

    monkeypatch.setattr(routes_mod, "_sse_with_retry", _half_iter)
    r = client.post("/v1/chat/completions",
                    json={"model": "glm-x", "stream": True,
                          "messages": [{"role": "user", "content": "hi"}]},
                    headers=_auth())
    assert r.status_code == 200
    frames = [ln[6:] for ln in r.text.splitlines() if ln.startswith("data: ")]
    assert json.loads(frames[0])["choices"][0]["delta"]["content"] == "你"
    err = json.loads(frames[-1])
    assert err["error"]["code"] == 403
    assert "请求包含违规内容" in err["error"]["message"]
