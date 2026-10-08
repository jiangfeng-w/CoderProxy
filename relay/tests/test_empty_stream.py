"""上游空响应（空流）防御 keyless 单测——BUG-001 修复验收（2026-10-08）。

背景：牛码网关间歇性返回 HTTP 200 后立即干净关流（0 token 空流），此前 relay
如实转发成「成功但空」的响应/空 JSON（假成功）。本修复：
- 判定层（oai_adapter）：空响应判定先于产出；耗尽抛 EmptyStreamError；
- 路由层（routes）：空响应重试独立预算（不与 401 刷新共用），耗尽必抛并 502 透传；
- 诊断：上游原始行尾巴只进日志（chat_error detail），不进面向 agent 的错误体。

覆盖（对照 已知问题.md BUG-001 §7 验收）：
1. 默认档持续空流：必须 raise + 落库 chat_error（非 chat_done）；
2. 上游「空, 空, 成功」：客户端拿到第 3 次的内容，且 role 帧不重复；
3. 401 刷新路径（retries=0 与 =3）不得回归；
4. 面向 agent 的错误体不含上游原生行（message_stop 等）。

全部走假 provider / fake httpx client，不触发牛码网络请求。
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.models.providers.ta3 import Ta3Provider
from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

from relay import db, oai_adapter, routes as routes_mod, storage, tool_disguise
from relay.config import settings
from relay.routes import app as relay_app


def _run(coro):
    return asyncio.run(coro)


def _auth():
    return {"Authorization": "Bearer empty-test-key"}


def _ctx():
    return tool_disguise.build_disguise_context(None, "hybrid")


_MODEL = {"name": "glm-x", "api_key": "k", "base_url": "b", "anthropic": False}


# ─────────────────────────── 替身 ───────────────────────────

class _FakeStreamProvider:
    """stream_structured 替身：按预置批次逐次产出（末批重复），记录调用次数。"""

    _model_name = "fake"

    def __init__(self, batches: list[list[dict]]):
        self._batches = list(batches)
        self.calls = 0

    async def stream_structured(self, request):
        batch = self._batches[min(self.calls, len(self._batches) - 1)]
        self.calls += 1
        for e in batch:
            yield e


class _FakeChatProvider:
    """chat 替身：按预置响应逐次返回（末项重复），记录调用次数。"""

    _model_name = "fake"

    def __init__(self, responses: list):
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, request):
        r = self._responses[min(self.calls, len(self._responses) - 1)]
        self.calls += 1
        if isinstance(r, Exception):
            raise r
        return r


_EMPTY_TAIL = ['data: {"type":"message_stop"}']
_EMPTY_DONE = {"type": "done", "content": None, "thinking": None, "tool_calls": [],
               "finish_reason": "stop", "usage": Usage(), "raw_tail": list(_EMPTY_TAIL)}
_OK_DONE = {"type": "done", "content": "hi", "thinking": None, "tool_calls": [],
            "finish_reason": "stop",
            "usage": Usage(prompt_tokens=5, completion_tokens=2, total_tokens=7),
            "raw_tail": None}
_OK_STREAM = [{"type": "content", "delta": "ok"}, _OK_DONE]
_UNAUTHORIZED = RuntimeError('模型请求失败 401：{"error":"unauthorized"}')


async def _no_wait(attempt):
    return None


@pytest.fixture()
def fast_retry(monkeypatch):
    """去掉重试退避等待，测试不实际 sleep。"""
    monkeypatch.setattr(oai_adapter, "empty_stream_retry_wait", _no_wait)


# ─────────────────────────── 判定层：空响应口径 ───────────────────────────

def test_is_empty_done_variants():
    assert oai_adapter._is_empty_done(_EMPTY_DONE) is True
    assert oai_adapter._is_empty_done({**_EMPTY_DONE, "content": "hi"}) is False
    assert oai_adapter._is_empty_done({**_EMPTY_DONE, "thinking": "t"}) is False
    # usage 有量（即便 1 token）→ 非空
    assert oai_adapter._is_empty_done(
        {**_EMPTY_DONE, "usage": Usage(prompt_tokens=1, total_tokens=1)}) is False
    # usage 缺失视为空
    assert oai_adapter._is_empty_done({**_EMPTY_DONE, "usage": None}) is True


def test_is_empty_done_tool_call_shell_still_empty():
    """上游 tool_use 只发 content_block_start（id/name）就关流：arguments={} 的壳
    tool_call + usage 全 0 仍判空（占位产出对客户端无用，应重试）。"""
    shell = {"id": "toolu_1", "name": "Bash", "arguments": {}}
    assert oai_adapter._tool_call_is_shell(shell) is True
    assert oai_adapter._tool_call_is_shell({"id": "x", "name": "n"}) is True
    assert oai_adapter._tool_call_is_shell(
        {"id": "x", "name": "n", "arguments": {"_raw": ""}}) is True
    # 有真实 arguments（含非空 _raw 降级）→ 非壳
    assert oai_adapter._tool_call_is_shell(
        {"id": "x", "name": "n", "arguments": {"command": "ls"}}) is False
    assert oai_adapter._tool_call_is_shell(
        {"id": "x", "name": "n", "arguments": {"_raw": "not-json"}}) is False
    # 壳 tool_call → 空响应；真实 tool_call → 非空
    assert oai_adapter._is_empty_done({**_EMPTY_DONE, "tool_calls": [shell]}) is True
    assert oai_adapter._is_empty_done(
        {**_EMPTY_DONE, "tool_calls": [{"id": "x", "name": "n", "arguments": {"c": 1}}]}
    ) is False


def test_is_empty_chat_response_variants():
    assert oai_adapter._is_empty_chat_response(
        ChatResponse(finish_reason="stop", usage=Usage())) is True
    assert oai_adapter._is_empty_chat_response(
        ChatResponse(content="x", usage=Usage())) is False
    assert oai_adapter._is_empty_chat_response(
        ChatResponse(thinking="t", usage=Usage())) is False
    assert oai_adapter._is_empty_chat_response(
        ChatResponse(tool_calls=[{"id": "1", "name": "n", "arguments": {}}],
                     usage=Usage())) is True
    assert oai_adapter._is_empty_chat_response(
        ChatResponse(tool_calls=[{"id": "1", "name": "n", "arguments": {"c": 1}}],
                     usage=Usage())) is False


def test_empty_stream_error_and_message():
    err = oai_adapter.EmptyStreamError()
    assert oai_adapter._EMPTY_STREAM_MSG in str(err)
    assert err.raw_tail is None
    assert oai_adapter.empty_stream_exhausted_message(0) == oai_adapter._EMPTY_STREAM_MSG
    assert "已重试 3 次" in oai_adapter.empty_stream_exhausted_message(3)


# ─────────────────────────── 流式判定：空响应一帧不发即抛 ───────────────────────────

@pytest.mark.asyncio
async def test_stream_empty_raises_before_any_frame(fast_retry):
    """空响应流：判定先于产出——抛错时一帧未发（role 帧也不发）。"""
    p = _FakeStreamProvider([[_EMPTY_DONE]])
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    frames = []
    with pytest.raises(oai_adapter.EmptyStreamError) as ei:
        async for f in oai_adapter.stream_openai_sse(p, req):
            frames.append(f)
    assert frames == []  # 关键：零帧（旧实现先发 role 帧，重试会重复且响应头已发）
    assert ei.value.raw_tail == list(_EMPTY_TAIL)  # 原始行尾巴随异常供日志


@pytest.mark.asyncio
async def test_stream_nonempty_passes(fast_retry):
    """正常流不受空响应防御影响（role 帧 + 内容 + finish + [DONE]）。"""
    p = _FakeStreamProvider([_OK_STREAM])
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    frames = [f async for f in oai_adapter.stream_openai_sse(p, req)]
    role_frames = [f for f in frames if '"role": "assistant"' in f
                   or '"role":"assistant"' in f]
    assert len(role_frames) == 1
    assert any("ok" in f for f in frames)
    assert frames[-1] == "data: [DONE]\n\n"


# ─────────────────────────── 非流式：chat_with_empty_retry ───────────────────────────

@pytest.mark.asyncio
async def test_chat_empty_retry_succeeds(monkeypatch, fast_retry):
    monkeypatch.setattr(settings, "empty_stream_retries", 3)
    p = _FakeChatProvider([
        ChatResponse(finish_reason="stop", usage=Usage()),
        ChatResponse(content="ok", finish_reason="stop",
                     usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2)),
    ])
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    resp = await oai_adapter.chat_with_empty_retry(p, req)
    assert resp.content == "ok" and p.calls == 2


@pytest.mark.asyncio
async def test_chat_empty_retry_exhausted_raises(monkeypatch, fast_retry):
    monkeypatch.setattr(settings, "empty_stream_retries", 2)
    p = _FakeChatProvider([ChatResponse(finish_reason="stop", usage=Usage())])
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    with pytest.raises(oai_adapter.EmptyStreamError, match=oai_adapter._EMPTY_STREAM_MSG):
        await oai_adapter.chat_with_empty_retry(p, req)
    assert p.calls == 3  # 1 + 2 次重试


@pytest.mark.asyncio
async def test_chat_empty_retry_disabled(monkeypatch, fast_retry):
    monkeypatch.setattr(settings, "empty_stream_retries", 0)
    p = _FakeChatProvider([ChatResponse(finish_reason="stop", usage=Usage())])
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    with pytest.raises(oai_adapter.EmptyStreamError):
        await oai_adapter.chat_with_empty_retry(p, req)
    assert p.calls == 1


@pytest.mark.asyncio
async def test_chat_empty_retry_passes_through_401(monkeypatch, fast_retry):
    """真 401 不归空响应重试处理（原样抛给外层），不消耗空响应预算。"""
    monkeypatch.setattr(settings, "empty_stream_retries", 3)
    p = _FakeChatProvider([_UNAUTHORIZED])
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    with pytest.raises(RuntimeError) as ei:
        await oai_adapter.chat_with_empty_retry(p, req)
    assert not isinstance(ei.value, oai_adapter.EmptyStreamError)
    assert p.calls == 1


# ─────────────────────────── 路由层：预算拆分 + 耗尽必抛（全档位枚举） ───────────────────────────

def _patch_routes(monkeypatch, provider):
    monkeypatch.setattr(routes_mod, "build_provider", lambda *a, **kw: provider)

    async def _no_sync():
        return []

    monkeypatch.setattr(routes_mod.auth_flow, "sync_models", _no_sync)


@pytest.mark.asyncio
@pytest.mark.parametrize("retries", [0, 1, 2, 3, 5])
async def test_sse_with_retry_empty_exhausted_raises_all_tiers(
        tmp_path, monkeypatch, fast_retry, retries):
    """各档位持续空流：重试 1+N 次后**必抛**（BUG-001 核心回归——
    旧实现默认档 3 下会正常结束不抛错，落成假成功 chat_done）。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "empty_stream_retries", retries)
    await storage.save_models([_MODEL])
    p = _FakeStreamProvider([[_EMPTY_DONE]])
    _patch_routes(monkeypatch, p)

    req = ChatRequest(model="glm-x", messages=[ChatMessage(role="user", content="x")])
    with pytest.raises(oai_adapter.EmptyStreamError):
        async for _ in routes_mod._sse_with_retry("glm-x", req, False, _ctx()):
            pass
    assert p.calls == retries + 1  # 1 次首发 + N 次重试，预算不被 401 挤占


@pytest.mark.asyncio
async def test_sse_with_retry_empty_then_success(tmp_path, monkeypatch, fast_retry):
    """上游「空, 空, 成功」：客户端拿到第 3 次内容，role 帧只发一次。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "empty_stream_retries", 3)
    await storage.save_models([_MODEL])
    p = _FakeStreamProvider([[_EMPTY_DONE], [_EMPTY_DONE], _OK_STREAM])
    _patch_routes(monkeypatch, p)

    req = ChatRequest(model="glm-x", messages=[ChatMessage(role="user", content="x")])
    frames = [f async for f in routes_mod._sse_with_retry("glm-x", req, False, _ctx())]
    assert p.calls == 3
    assert any("ok" in f for f in frames)
    assert frames[-1] == "data: [DONE]\n\n"
    role_frames = [f for f in frames if '"role":"assistant"' in f.replace(" ", "")]
    assert len(role_frames) == 1  # 重试不重发 role 帧（旧实现每重试多吐一个）


@pytest.mark.asyncio
async def test_sse_with_retry_401_still_works_with_empty_retries(
        tmp_path, monkeypatch, fast_retry):
    """401 刷新路径在空响应防御引入后不得回归（档位 0 与 3 均验证）。"""
    for retries in (0, 3):
        monkeypatch.setattr(settings, "data_dir", str(tmp_path))
        monkeypatch.setattr(settings, "empty_stream_retries", retries)
        await storage.save_models([_MODEL])

        class _401ThenOk(_FakeStreamProvider):
            async def stream_structured(self, request):
                if self.calls == 0:
                    self.calls += 1
                    raise _UNAUTHORIZED
                self.calls += 1
                for e in _OK_STREAM:
                    yield e

        p = _401ThenOk([[]])
        _patch_routes(monkeypatch, p)
        req = ChatRequest(model="glm-x", messages=[ChatMessage(role="user", content="x")])
        frames = [f async for f in routes_mod._sse_with_retry("glm-x", req, False, _ctx())]
        assert p.calls == 2  # 401 后重试成功
        assert any("ok" in f for f in frames)
        assert frames[-1] == "data: [DONE]\n\n"


# ─────────────────────────── 端点：502 透传 + 落库 + 合规 ───────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "relay_log_max_rows", 100000)
    settings.relay_api_key = "empty-test-key"
    _run(storage.save_models([_MODEL]))
    _run(storage.set_model_whitelist([]))  # 空 = 全部启用

    async def _serving_on():
        return True

    monkeypatch.setattr(routes_mod, "_serving", _serving_on)

    async def _no_sync():
        return []

    monkeypatch.setattr(routes_mod.auth_flow, "sync_models", _no_sync)
    monkeypatch.setattr(oai_adapter, "empty_stream_retry_wait", _no_wait)
    with TestClient(relay_app, raise_server_exceptions=False) as c:
        yield c
    settings.relay_api_key = ""


def test_endpoint_stream_empty_502_not_fake_success(client, monkeypatch):
    """验收 1/4：流式持续空流（默认档 3）→ 502 头前透传，落 chat_error 非 chat_done；
    面向 agent 的错误体不得含上游原生行（raw_tail 只进日志）。"""
    monkeypatch.setattr(settings, "empty_stream_retries", 3)
    p = _FakeStreamProvider([[_EMPTY_DONE]])
    _patch_routes(monkeypatch, p)

    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-x", "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth())
    assert resp.status_code == 502
    body = resp.json()
    assert body["error"]["type"] == "upstream_empty_response"
    assert "已重试 3 次" in body["error"]["message"]
    # 合规（硬性规则 2）：错误体不含上游原生行
    assert "message_stop" not in resp.text
    assert "upstream_raw_tail" not in resp.text
    assert "data: " not in resp.text
    # 上游调用 1+3=4 次（预算不被 401 挤占）
    assert p.calls == 4

    errors, _ = _run(db.query_logs(kind="chat_error"))
    dones, _ = _run(db.query_logs(kind="chat_done"))
    assert len(errors) == 1 and len(dones) == 0
    # raw_tail 只进日志 detail（诊断价值保留在服务端）
    detail = errors[0]["detail"]
    assert detail["upstream_raw_tail"] == list(_EMPTY_TAIL)
    assert "message_stop" not in detail["error"]


def test_endpoint_stream_empty_then_success_delivers_content(client, monkeypatch):
    """验收 2：上游「空, 空, 成功」→ 客户端拿到第 3 次内容 + 正常收尾。"""
    monkeypatch.setattr(settings, "empty_stream_retries", 3)
    p = _FakeStreamProvider([[_EMPTY_DONE], [_EMPTY_DONE], _OK_STREAM])
    _patch_routes(monkeypatch, p)

    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-x", "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth())
    assert resp.status_code == 200
    assert "ok" in resp.text
    assert "data: [DONE]" in resp.text
    assert resp.text.count('"role":"assistant"') + resp.text.count('"role": "assistant"') == 1
    assert p.calls == 3
    dones, _ = _run(db.query_logs(kind="chat_done"))
    assert len(dones) == 1


def test_endpoint_nonstream_empty_502_not_fake_success(client, monkeypatch):
    """非流式持续空流：502 透传（不再回假成功空 JSON），落 chat_error。"""
    monkeypatch.setattr(settings, "empty_stream_retries", 3)
    p = _FakeChatProvider([ChatResponse(finish_reason="stop", usage=Usage())])
    _patch_routes(monkeypatch, p)

    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-x",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth())
    assert resp.status_code == 502
    assert resp.json()["error"]["type"] == "upstream_empty_response"
    assert p.calls == 4

    errors, _ = _run(db.query_logs(kind="chat_error"))
    dones, _ = _run(db.query_logs(kind="chat_done"))
    assert len(errors) == 1 and errors[0]["stream"] == 0
    assert len(dones) == 0


def test_endpoint_401_refresh_end_to_end(client, monkeypatch):
    """验收 3（端到端）：上游「401, 成功」→ 目录同步 1 次、落 auth_401_refresh、
    客户端拿到内容（档位 0 与 3 均不回归）。"""
    refreshes_before = 0
    for retries in (0, 3):
        monkeypatch.setattr(settings, "empty_stream_retries", retries)

        class _401ThenOk(_FakeStreamProvider):
            async def stream_structured(self, request):
                if self.calls == 0:
                    self.calls += 1
                    raise _UNAUTHORIZED
                self.calls += 1
                for e in _OK_STREAM:
                    yield e

        p = _401ThenOk([[]])
        _patch_routes(monkeypatch, p)
        resp = client.post("/v1/chat/completions",
                           json={"model": "glm-x", "stream": True,
                                 "messages": [{"role": "user", "content": "hi"}]},
                           headers=_auth())
        assert resp.status_code == 200
        assert "ok" in resp.text
        assert p.calls == 2
        refreshes, _ = _run(db.query_logs(kind="auth_401_refresh"))
        assert len(refreshes) == refreshes_before + 1  # 本轮恰刷新 1 次
        refreshes_before += 1


# ─────────────────────────── ta3.py：空响应原始行尾巴 ───────────────────────────

def _provider(model: str = "kimi-k3", anthropic: bool = True) -> Ta3Provider:
    return Ta3Provider(api_key="llm-x", base_url="https://lc.yinhaiyun.com/newcoder",
                       model=model, meta={"anthropic": anthropic})


class _FakeResp:
    status_code = 200

    def __init__(self, lines: list[str]):
        self._lines = lines

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def aiter_lines(self):
        async def _gen():
            for ln in self._lines:
                yield ln
        return _gen()

    async def aread(self):
        return b""


def _patch_client(monkeypatch, p: Ta3Provider, lines: list[str]):
    class _Client:
        def stream(self, *a, **kw):
            return _FakeResp(lines)

    monkeypatch.setattr(p, "_client", _Client())


@pytest.mark.asyncio
async def test_empty_done_attaches_raw_tail(monkeypatch):
    """空响应流：done 携带上游原始行尾巴（环形缓冲）。"""
    p = _provider()
    _patch_client(monkeypatch, p, ['data: {"type":"message_stop"}', "data: [DONE]"])
    events = [e async for e in p.stream_structured(
        ChatRequest(model="kimi-k3", messages=[ChatMessage(role="user", content="x")]))]
    dones = [e for e in events if e["type"] == "done"]
    # message_stop 为终止帧，其后 [DONE] 行不再被读取（真实网关 Anthropic 协议也不发）
    assert dones and dones[0]["raw_tail"] == ['data: {"type":"message_stop"}']
    assert dones[0]["content"] is None and dones[0]["usage"].total_tokens == 0


@pytest.mark.asyncio
async def test_nonempty_done_raw_tail_is_none(monkeypatch):
    """正常流：done 的 raw_tail 为 None（不污染正常路径）。"""
    p = _provider()
    _patch_client(monkeypatch, p, [
        'data: {"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"你好"}}',
        'data: {"type":"message_stop"}',
    ])
    events = [e async for e in p.stream_structured(
        ChatRequest(model="kimi-k3", messages=[ChatMessage(role="user", content="x")]))]
    dones = [e for e in events if e["type"] == "done"]
    assert dones and dones[0]["raw_tail"] is None
    assert dones[0]["content"] == "你好"


@pytest.mark.asyncio
async def test_empty_done_ring_buffer_caps_lines(monkeypatch):
    """原始行尾巴环形截断：只保留最近 _RAW_TAIL_MAX 行，单行截断到 _RAW_TAIL_LINE_MAX。"""
    from app.models.providers import ta3 as ta3_mod
    p = _provider()
    long_line = 'data: {"type":"unknown","payload":"' + "x" * 600 + '"}'
    lines = ([f'data: {{"type":"noise","i":{i}}}' for i in range(20)]
             + [long_line, 'data: {"type":"message_stop"}'])
    _patch_client(monkeypatch, p, lines)
    events = [e async for e in p.stream_structured(
        ChatRequest(model="kimi-k3", messages=[ChatMessage(role="user", content="x")]))]
    tail = [e for e in events if e["type"] == "done"][0]["raw_tail"]
    assert len(tail) <= ta3_mod._RAW_TAIL_MAX
    assert all(len(ln) <= ta3_mod._RAW_TAIL_LINE_MAX for ln in tail)
    assert tail[-1] == 'data: {"type":"message_stop"}'
