"""SSE 分帧 / 工具 schema 的 keyless 单测（不触发牛码网络请求）。"""
import json

import pytest

from app.models.providers.ta3_tool_schemas import disguise_tools
from app.models.schemas import ChatMessage, ChatRequest, Usage

from relay.oai_adapter import stream_openai_sse


class _FakeProvider:
    """仅实现 stream_structured，产出预置事件。"""

    def __init__(self, events):
        self._events = events
        self._model_name = "m"

    async def stream_structured(self, request):
        for e in self._events:
            yield e


def _collect(events, include_usage=True):
    p = _FakeProvider(events)
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    return stream_openai_sse(p, req, include_usage=include_usage)


@pytest.mark.asyncio
async def test_sse_frame_sequence():
    events = [
        {"type": "thinking", "delta": "think-1"},
        {"type": "content", "delta": "hello"},
        {"type": "done", "content": "hello", "thinking": "think-1",
         "tool_calls": [], "finish_reason": "stop",
         "usage": Usage(prompt_tokens=3, completion_tokens=5, total_tokens=8,
                        reasoning_tokens=2, cached_input_tokens=1)},
    ]
    frames = [f async for f in _collect(events, include_usage=True)]

    # 首帧：声明 assistant 角色
    first = json.loads(frames[0][6:].strip())  # 去掉 "data: " 前缀
    assert first["choices"][0]["delta"] == {"role": "assistant", "content": ""}

    # thinking → reasoning_content
    t = json.loads(frames[1][6:].strip())
    assert t["choices"][0]["delta"]["reasoning_content"] == "think-1"

    # content
    c = json.loads(frames[2][6:].strip())
    assert c["choices"][0]["delta"]["content"] == "hello"

    # finish 帧
    f = json.loads(frames[3][6:].strip())
    assert f["choices"][0]["finish_reason"] == "stop"

    # usage 帧
    u = json.loads(frames[4][6:].strip())
    assert u["choices"] == []
    assert u["usage"]["prompt_tokens"] == 3
    assert u["usage"]["completion_tokens_details"]["reasoning_tokens"] == 2

    # [DONE]
    assert frames[-1].strip() == "data: [DONE]"


@pytest.mark.asyncio
async def test_sse_tool_calls_frames():
    events = [
        {"type": "done", "content": None, "thinking": None,
         "tool_calls": [{"id": "c1", "name": "terminal_exec", "arguments": {"command": "echo hi"}}],
         "finish_reason": "tool_calls", "usage": Usage()},
    ]
    frames = [f async for f in _collect(events, include_usage=False)]
    # 首帧 role + 工具帧 + finish + [DONE]
    tool_frame = json.loads(frames[1][6:].strip())
    tc = tool_frame["choices"][0]["delta"]["tool_calls"][0]
    assert tc["function"]["name"] == "terminal_exec"
    assert json.loads(tc["function"]["arguments"]) == {"command": "echo hi"}
    assert tool_frame["choices"][0]["finish_reason"] is None
    assert frames[-1].strip() == "data: [DONE]"


def test_disguise_tools_maps_native():
    """出站工具 schema：chatcoder 内部名 terminal_exec → ta3 原生 Bash schema。

    M2 阶段沿用 vendored ta3.py 的 strict 式 disguise_tools（只认 chatcoder 内部
    工具名）；第三方 agent 常见工具名（bash/read_file 等）的扩展映射归 M3。
    """
    tools = [{"type": "function", "function": {"name": "terminal_exec", "parameters": {"type": "object"}}}]
    out = disguise_tools(tools)
    assert out and out[0]["function"]["name"] == "Bash"
    # 未映射工具在 strict 式下剔除（如第三方 agent 的 glob_file_search / bash）
    tools2 = [{"type": "function", "function": {"name": "glob_file_search", "parameters": {}}}]
    assert disguise_tools(tools2) == []
