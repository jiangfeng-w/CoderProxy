"""WorkBuddy 聊天 adapter 单测：请求改写 / 头族 / 帧重建 / 错误映射 / 账号池轮转。

全程不触网：adapter 以 httpx.MockTransport 注入（构造参数 transport）；
账号池落 tmp 目录（monkeypatch settings.data_dir）。
"""
import asyncio
import json

import httpx
import pytest

from app.models.schemas import ChatMessage, ChatRequest
from relay.config import settings
from relay.platforms import store
from relay.platforms.base import PlatformUpstreamError
from relay.platforms.workbuddy import chat as wbchat

UPSTREAM = f"{settings.wb_api_base}{wbchat.CHAT_PATH}"

_SSE_OK = [
    'data: {"id":"cmb-1","model":"glm-5.3-flash","choices":[{"index":0,'
    '"delta":{"role":"assistant","content":"","reasoning_content":"想"},"finish_reason":""}]}',
    'data: {"id":"cmb-1","model":"glm-5.3-flash","choices":[{"index":0,'
    '"delta":{"content":"你好","function_call":null,"refusal":"","extra_fields":null},'
    '"finish_reason":""}]}',
    'data: {"id":"cmb-1","model":"glm-5.3-flash","choices":[{"index":0,'
    '"delta":{"content":"","tool_calls":[]},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":18,"completion_tokens":87,"total_tokens":105,'
    '"completion_tokens_details":{"reasoning_tokens":72}}}',
    "data: [DONE]",
]

_SSE_TOOLS = [
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"id":"call_47","type":"function",'
    '"function":{"name":"get_weather","arguments":"{\\""},"index":0}]},"finish_reason":""}]}',
    'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"type":"function",'
    '"function":{"name":"","arguments":"city\\":\\"Beijing\\"}"},"index":0}]},'
    '"finish_reason":""}]}',
    'data: {"choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}],'
    '"usage":{"prompt_tokens":5,"completion_tokens":9,"total_tokens":14}}',
    "data: [DONE]",
]


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    wbchat.reset_cursor()
    return tmp_path


def _transport(handler):
    return httpx.MockTransport(handler)


async def _seed_account(uid="u_wb1", **extra):
    await store.upsert_account("workbuddy", {
        "uid": uid, "access_token": "at", "refresh_token": "rt",
        "expires_at": 9_999_999_999_999,  # 远期：不触发刷新
        **extra})
    account = await store.find_account("workbuddy", uid)
    assert account is not None
    return account


def _sse_response(lines, status=200):
    body = "".join(f"{ln}\n\n" for ln in lines).encode()
    return httpx.Response(status, content=body,
                          headers={"Content-Type": "text/event-stream"})


def _adapter(handler, model="glm-5.3-flash"):
    return wbchat.WorkBuddyChatAdapter(model, transport=_transport(handler))


REQ = ChatRequest(model="glm-5.3-flash",
                  messages=[ChatMessage(role="user", content="hi")])


# ─────────────────────────── transform_request 钩子 ───────────────────────────

def test_transform_forces_stream_and_keeps_tools():
    a = _adapter(lambda r: _sse_response(["data: [DONE]"]))
    req = ChatRequest(
        model="m", messages=[ChatMessage(role="user", content="hi")],
        tools=[{"type": "function", "function": {"name": "get_weather"}}],
        max_tokens=128, temperature=0.3)
    body = a.transform_request(req)
    assert body["stream"] is True  # B2：强制流式
    assert body["model"] == "glm-5.3-flash"  # 裸名直发上游
    assert body["tools"][0]["function"]["name"] == "get_weather"  # B5 原生透传
    assert body["max_tokens"] == 128 and body["temperature"] == 0.3
    assert "reasoning_effort" not in body  # 未传档位 → 不带（上游默认思考）


def test_transform_effort_none_keeps_without_summary():
    a = _adapter(lambda r: _sse_response(["data: [DONE]"]))
    body = a.transform_request(ChatRequest(
        model="m", messages=[ChatMessage(role="user", content="hi")],
        thinking=True, reasoning_effort="none"))
    assert body["reasoning_effort"] == "none"  # B4：none 真关（上游识别）
    assert "reasoning_summary" not in body  # 关思考不配对 summary


def test_transform_effort_high_pairs_summary_auto():
    a = _adapter(lambda r: _sse_response(["data: [DONE]"]))
    body = a.transform_request(ChatRequest(
        model="m", messages=[ChatMessage(role="user", content="hi")],
        thinking=True, reasoning_effort="high"))
    assert body["reasoning_effort"] == "high"
    assert body["reasoning_summary"] == "auto"  # B4：暴露思考的配对参数


def test_transform_effort_off_dropped():
    a = _adapter(lambda r: _sse_response(["data: [DONE]"]))
    body = a.transform_request(ChatRequest(
        model="m", messages=[ChatMessage(role="user", content="hi")],
        thinking=True, reasoning_effort="off"))
    assert "reasoning_effort" not in body  # B4：off 上游不认识 → 删（落默认）


def test_transform_system_neutralized():
    """B3：agent 身份句/超长 system → 中性句；普通短 system 原样。"""
    a = _adapter(lambda r: _sse_response(["data: [DONE]"]))
    body = a.transform_request(ChatRequest(
        model="m", messages=[
            ChatMessage(role="system",
                        content="You are Claude Code, Anthropic's official CLI for Claude."),
            ChatMessage(role="user", content="hi")]))
    assert body["messages"][0]["content"] == wbchat.NEUTRAL_SYSTEM_PROMPT

    body = a.transform_request(ChatRequest(
        model="m", messages=[
            ChatMessage(role="system", content="You are a coding assistant. " * 100),
            ChatMessage(role="user", content="hi")]))
    assert body["messages"][0]["content"] == wbchat.NEUTRAL_SYSTEM_PROMPT

    keep = "请用中文回答，保持简洁。"
    body = a.transform_request(ChatRequest(
        model="m", messages=[ChatMessage(role="system", content=keep),
                             ChatMessage(role="user", content="hi")]))
    assert body["messages"][0]["content"] == keep


def test_transform_system_block_shape_kept():
    """块状 system 命中 → 块形状保持（文本块替换）。"""
    a = _adapter(lambda r: _sse_response(["data: [DONE]"]))
    body = a.transform_request(ChatRequest(
        model="m", messages=[
            ChatMessage(role="system", content=None,
                        content_blocks=[{"type": "text",
                                         "text": "cc_entrypoint=cli blah"}]),
            ChatMessage(role="user", content="hi")]))
    blocks = body["messages"][0]["content"]
    assert isinstance(blocks, list) and blocks[0]["text"] == wbchat.NEUTRAL_SYSTEM_PROMPT


# ─────────────────────────── 头族（B1 桌面档） ───────────────────────────

def test_headers_desktop_tier(data_dir):
    _run(_seed_account(domain="www.codebuddy.cn"))
    a = _adapter(lambda r: _sse_response(["data: [DONE]"]))
    account = _run(store.find_account("workbuddy", "u_wb1"))
    h = a._headers(account)
    assert h["User-Agent"] == settings.wb_user_agent  # 三段式桌面档
    assert h["X-Agent-Purpose"] == "conversation"
    assert h["X-IDE-Name"] == "WorkBuddy" and h["X-IDE-Type"] == "WorkBuddy"
    assert h["X-Product"] == "WorkBuddy"
    assert h["X-CodeBuddy-Request"] == "1"
    assert h["Authorization"] == "Bearer at"
    assert h["X-User-Id"] == "u_wb1"
    assert h["X-Domain"] == "www.codebuddy.cn"
    # 每号稳定设备头（36 hex）
    from relay.platforms.workbuddy.client import derive_stable_id
    assert h["X-Machine-ID"] == derive_stable_id("u_wb1", "machine")
    assert h["X-Session-ID"] == derive_stable_id("u_wb1", "session")


# ─────────────────────────── 帧重建与 usage ───────────────────────────

def test_stream_content_thinking_usage(data_dir):
    _run(_seed_account())
    a = _adapter(lambda r: _sse_response(_SSE_OK))
    events = _run(_collect(a, REQ))
    kinds = [e["type"] for e in events]
    assert kinds[:2] == ["thinking", "content"]
    assert events[0]["delta"] == "想" and events[1]["delta"] == "你好"
    done = events[-1]
    assert done["type"] == "done" and done["content"] == "你好"
    assert done["thinking"] == "想"
    assert done["finish_reason"] == "stop"
    assert done["usage"].prompt_tokens == 18
    assert done["usage"].completion_tokens == 87
    assert done["usage"].reasoning_tokens == 72  # B8：completion_tokens_details 映射


def test_stream_tool_calls_merged(data_dir):
    _run(_seed_account())
    a = _adapter(lambda r: _sse_response(_SSE_TOOLS))
    events = _run(_collect(a, REQ))
    done = events[-1]
    tcs = done["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["id"] == "call_47"
    assert tcs[0]["name"] == "get_weather"
    assert tcs[0]["arguments"] == {"city": "Beijing"}  # 分片拼接后解析
    assert done["finish_reason"] == "tool_calls"


def test_internal_fields_stripped(data_dir):
    """硬性规则 2：上游内部字段（function_call/refusal/extra_fields）不出现在事件里。"""
    _run(_seed_account())
    a = _adapter(lambda r: _sse_response(_SSE_OK))
    events = _run(_collect(a, REQ))
    text = json.dumps(events, ensure_ascii=False, default=str)
    assert "function_call" not in text and "extra_fields" not in text
    assert "refusal" not in text and "logprobs" not in text


def test_chat_nonstream_aggregates(data_dir):
    """非流式：内部收流聚合（B2：上游仅流式）。"""
    _run(_seed_account())
    a = _adapter(lambda r: _sse_response(_SSE_OK))
    resp = _run(a.chat(REQ))
    assert resp.content == "你好" and resp.thinking == "想"
    assert resp.finish_reason == "stop"
    assert resp.usage.total_tokens == 105
    assert resp.model == "glm-5.3-flash"


# ─────────────────────────── 错误映射（B6） ───────────────────────────

@pytest.mark.parametrize("code,status,expected", [
    (11101, 400, 400),   # 参数错/非流式
    (11128, 400, 400),   # 渠道风控
    (11102, 200, 404),   # 模型不可用（SSE 内错误信封）
    (6004, 429, 429),    # 限流
])
def test_parse_error_mapping(code, status, expected):
    a = _adapter(lambda r: _sse_response(["data: [DONE]"]))
    err = a.parse_error(status, json.dumps({"code": code, "msg": "测试错误"}))
    assert isinstance(err, PlatformUpstreamError)
    assert err.status == expected


def test_parse_error_unknown_passthrough():
    a = _adapter(lambda r: _sse_response(["data: [DONE]"]))
    err = a.parse_error(500, "boom")
    assert err.status == 500
    err = a.parse_error(302, "weird")
    assert err.status == 502  # 非法状态码回落 502


def test_http_error_raises_mapped(data_dir):
    _run(_seed_account())
    a = _adapter(lambda r: httpx.Response(400, json={
        "code": 11128, "msg": "Illegal API invocation from an unapproved channel"}))
    with pytest.raises(PlatformUpstreamError) as ei:
        _run(_collect(a, REQ))
    assert ei.value.status == 400
    assert "11128" in str(ei.value)


# ─────────────────────────── 账号池轮转（§4.3） ───────────────────────────

def test_no_account_raises_no_available(data_dir):
    a = _adapter(lambda r: _sse_response(_SSE_OK))
    with pytest.raises(wbchat.NoAvailableAccountError):
        _run(_collect(a, REQ))


def test_rotation_picks_across_accounts(data_dir):
    """轮询：两次请求命中不同账号（uid 排序 + 游标）。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    seen: list[str] = []

    def handler(request):
        seen.append(request.headers.get("X-User-Id", ""))
        return _sse_response(_SSE_OK)

    a = _adapter(handler)
    _run(_collect(a, REQ))
    a2 = _adapter(handler)
    _run(_collect(a2, REQ))
    assert seen == ["u_a", "u_b"]  # 游标递增 → 换号


def test_rotation_on_credential_rejected(data_dir, monkeypatch):
    """单号 401 → 换下一号（同请求内）；成功即返回。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    calls: list[str] = []

    def handler(request):
        uid = request.headers.get("X-User-Id", "")
        calls.append(uid)
        if uid == "u_a":
            return httpx.Response(401, json={"code": 401, "msg": "token expired"})
        return _sse_response(_SSE_OK)

    # 账号层刷新不应触发（expires_at 远期）；被拒后直接换号
    a = _adapter(handler)
    events = _run(_collect(a, REQ))
    assert calls == ["u_a", "u_b"]
    assert events[-1]["content"] == "你好"


def test_all_accounts_rejected_raises_no_available(data_dir):
    """全部号被拒 → 池不可用语义（NoAvailableAccount，routes 映射 503 + Retry-After）。"""
    _run(_seed_account(uid="u_a"))
    _run(_seed_account(uid="u_b"))
    a = _adapter(lambda r: httpx.Response(401, json={"code": 401, "msg": "expired"}))
    with pytest.raises(wbchat.NoAvailableAccountError):
        _run(_collect(a, REQ))


def test_refresh_credentials_refreshes_active(data_dir, monkeypatch):
    """refresh_credentials：刷新 active 号（oauth.refresh_token 打桩）。"""
    _run(_seed_account(uid="u_a"))
    a = _adapter(lambda r: _sse_response(_SSE_OK))
    a._active_uid = "u_a"
    refreshed: list[str] = []

    async def _fake_refresh(account):
        refreshed.append(str(account.get("uid")))
        return {**account, "access_token": "new"}

    monkeypatch.setattr(wbchat.oauth, "refresh_token", _fake_refresh)
    ok = _run(a.refresh_credentials())
    assert ok is True and refreshed == ["u_a"]


# ─────────────────────────── 工具函数 ───────────────────────────

def test_parse_frame_ignores_heartbeat_and_bad_lines():
    assert wbchat._parse_frame(": heartbeat") is None
    assert wbchat._parse_frame("") is None
    assert wbchat._parse_frame("garbage") is None
    assert wbchat._parse_frame("data: not-json") is None
    assert wbchat._parse_frame("data: [DONE]") is None


def test_parse_frame_error_envelope():
    frame = wbchat._parse_frame('data: {"code":11102,"msg":"model not found"}')
    assert "__error__" in frame


def test_message_payload_tool_roundtrip():
    payload = wbchat._message_payload(ChatMessage(
        role="assistant", content=None,
        tool_calls=[{"id": "c1", "name": "get_weather",
                     "arguments": {"city": "Beijing"}}]))
    assert payload["tool_calls"][0]["function"]["arguments"] == '{"city": "Beijing"}'
    payload = wbchat._message_payload(ChatMessage(
        role="tool", content="result", tool_call_id="c1"))
    assert payload["tool_call_id"] == "c1"


async def _collect(adapter, request):
    return [e async for e in adapter.stream_structured(request)]
