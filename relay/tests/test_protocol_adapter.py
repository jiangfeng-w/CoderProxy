"""多协议端点兼容（三协议入站统一）keyless 单测。

覆盖（对照需求文档 §7 验收 2/3 + §4 映射表）：
1. 识别层 detect_protocol：路径 + body 双判（含 Cursor CLI 畸形 case）；
2. Responses → ChatRequest 字段映射：instructions/input[]/function_call/
   function_call_output/custom_tool_call/reasoning 丢弃/encrypted_content 清理/tools 转换；
3. Anthropic → ChatRequest 字段映射：顶层 system（含 billing header）/tool_use/
   tool_result/thinking/非法 id 补齐/tools input_schema 转换；
4. 响应回译：Responses output[] 非流式 + typed 事件流；Anthropic 非流式 + typed 事件流；
5. 端点矩阵：三端点路由可用 + x-api-key 鉴权 + 错误体协议形状（全走假 provider）。

全部不触发牛码网络请求。
"""
import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

from relay import protocol_adapter as pa
from relay import routes as routes_mod, storage
from relay.config import settings
from relay.routes import app as relay_app


def _run(coro):
    return asyncio.run(coro)


def _auth():
    return {"Authorization": "Bearer proto-test-key"}


def _anthropic_auth():
    return {"x-api-key": "proto-test-key"}


def _model():
    return {"name": "glm-x", "api_key": "k", "base_url": "b", "anthropic": False}


_MODEL = {"name": "glm-x", "api_key": "k", "base_url": "b", "anthropic": False}


# ═══════════════════════ 1. 识别层 ═══════════════════════

def test_detect_protocol_by_path():
    assert pa.detect_protocol("/v1/responses", {}) == pa.PROTOCOL_RESPONSES
    assert pa.detect_protocol("/responses", {}) == pa.PROTOCOL_RESPONSES
    assert pa.detect_protocol("/v1/messages", {}) == pa.PROTOCOL_ANTHROPIC
    assert pa.detect_protocol("/v1/messages/count_tokens", {}) == pa.PROTOCOL_ANTHROPIC
    assert pa.detect_protocol("/v1/chat/completions", {"messages": []}) == pa.PROTOCOL_CHAT


def test_detect_protocol_cursor_cli_malformed_case():
    """Cursor CLI 把 Responses body（input[]）打到 chat 端点 → 容忍按 Responses 解析。"""
    body = {"model": "m", "input": [{"role": "user", "content": "hi"}], "stream": True}
    assert pa.detect_protocol("/v1/chat/completions", body) == pa.PROTOCOL_RESPONSES
    # 有 messages 的正常 chat body 不受影响
    assert pa.detect_protocol("/v1/chat/completions", {"messages": []}) == pa.PROTOCOL_CHAT


def test_detect_protocol_by_body_fallback():
    assert pa.detect_protocol("/unknown", {"input": "hi"}) == pa.PROTOCOL_RESPONSES
    # 顶层独立 system + Anthropic 专属块类型 → Anthropic
    assert pa.detect_protocol("/unknown", {
        "system": "s",
        "messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "x",
                                                         "name": "f", "input": {}}]}],
    }) == pa.PROTOCOL_ANTHROPIC
    # 普通 messages（无顶层 system）→ Chat
    assert pa.detect_protocol("/unknown", {
        "messages": [{"role": "user", "content": "hi"}]}) == pa.PROTOCOL_CHAT


# ═══════════════════════ 2. Responses 入站映射 ═══════════════════════

def test_responses_basic_instructions_and_input_string():
    req = pa.responses_to_chat_request({
        "model": "m", "instructions": "你是助手",
        "input": "你好", "stream": True,
    })
    assert req.model == "m"
    assert req.stream is True
    assert [m.role for m in req.messages] == ["system", "user"]
    assert req.messages[0].content == "你是助手"
    assert req.messages[1].content == "你好"


def test_responses_input_array_items():
    """message（user/assistant/developer）+ input_text/output_text 拆分。"""
    req = pa.responses_to_chat_request({
        "model": "m",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "问"}]},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "答"}]},
            {"type": "message", "role": "developer",
             "content": [{"type": "input_text", "text": "约束"}]},
        ],
    })
    assert [(m.role, m.content) for m in req.messages] == [
        ("user", "问"), ("assistant", "答"), ("developer", "约束")]


def test_responses_function_call_and_output_roundtrip():
    """function_call → assistant.tool_calls；function_call_output → role=tool。"""
    req = pa.responses_to_chat_request({
        "model": "m",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "查天气"}]},
            {"type": "function_call", "call_id": "call_1", "name": "get_weather",
             "arguments": '{"city": "北京"}'},
            {"type": "function_call_output", "call_id": "call_1", "output": "晴 25°C"},
        ],
    })
    assert len(req.messages) == 3
    assistant = req.messages[1]
    assert assistant.role == "assistant"
    assert assistant.tool_calls[0]["name"] == "get_weather"
    assert assistant.tool_calls[0]["arguments"] == {"city": "北京"}
    tool_msg = req.messages[2]
    assert tool_msg.role == "tool" and tool_msg.tool_call_id == "call_1"
    assert tool_msg.content == "晴 25°C"


def test_responses_parallel_function_calls_grouped():
    """并行 function_call 合并进同一条 assistant（9router currentAssistantMsg 语义）。"""
    req = pa.responses_to_chat_request({
        "model": "m",
        "input": [
            {"type": "function_call", "call_id": "c1", "name": "a", "arguments": "{}"},
            {"type": "function_call", "call_id": "c2", "name": "b", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "1"},
            {"type": "function_call_output", "call_id": "c2", "output": "2"},
        ],
    })
    assert len(req.messages) == 3  # 1 assistant + 2 tool
    assert [tc["name"] for tc in req.messages[0].tool_calls] == ["a", "b"]


def test_responses_nameless_function_call_skipped():
    """无名 function_call 丢弃（上游拒绝，9router #444）。"""
    req = pa.responses_to_chat_request({
        "model": "m",
        "input": [{"type": "function_call", "call_id": "c1", "name": "  ",
                   "arguments": "{}"}],
    })
    assert req.messages == []


def test_responses_reasoning_encrypted_content_stripped():
    """reasoning item（含 encrypted_content）不进 ChatRequest（stripContinuityFields）。"""
    req = pa.responses_to_chat_request({
        "model": "m",
        "input": [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "想"}],
             "encrypted_content": "SECRET_BLOB"},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
        ],
    })
    assert [m.role for m in req.messages] == ["user"]
    dumped = req.model_dump_json()
    assert "SECRET_BLOB" not in dumped


def test_responses_reasoning_text_attached_to_next_assistant():
    """reasoning 文本挂到紧随的 assistant（thinking 模型多轮回传要求），
    加密 blob 仍丢弃；非 assistant 消息到达则该文本失效（9router 同款）。"""
    req = pa.responses_to_chat_request({
        "model": "m",
        "input": [
            {"type": "reasoning",
             "summary": [{"type": "summary_text", "text": "先想"}],
             "encrypted_content": "BLOB"},
            {"type": "message", "role": "assistant",
             "content": [{"type": "output_text", "text": "答"}]},
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "丢弃"}]},
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "再问"}]},
        ],
    })
    assert req.messages[0].role == "assistant"
    assert req.messages[0].reasoning_content == "先想"
    # user 到达后 pending reasoning 失效
    assert req.messages[1].role == "user"
    assert req.messages[1].reasoning_content is None
    assert "BLOB" not in req.model_dump_json()


def test_responses_hosted_items_skipped():
    """web_search_call/computer_call 等无对应项跳过（不炸）。"""
    req = pa.responses_to_chat_request({
        "model": "m",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
            {"type": "web_search_call", "id": "ws_1", "status": "completed"},
            {"type": "computer_call", "id": "cc_1"},
        ],
    })
    assert [m.role for m in req.messages] == ["user"]


def test_responses_empty_input_gets_placeholder():
    """空 input[] 注入占位，避免上游以空 messages 拒绝（9router #389）。"""
    req = pa.responses_to_chat_request({"model": "m", "input": []})
    assert len(req.messages) == 1 and req.messages[0].content == "..."


def test_responses_tools_conversion():
    """Responses function 声明 → Chat function；hosted 工具丢弃；custom → input 壳。"""
    req = pa.responses_to_chat_request({
        "model": "m",
        "input": "hi",
        "tools": [
            {"type": "function", "name": "get_weather", "description": "查天气",
             "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}},
            {"type": "web_search"},
            {"type": "custom", "name": "run_code", "description": "跑代码"},
        ],
    })
    names = [t["function"]["name"] for t in req.tools]
    assert names == ["get_weather", "run_code"]
    assert req.tools[0]["function"]["parameters"]["properties"]["city"]["type"] == "string"
    assert req.tools[1]["function"]["parameters"]["required"] == ["input"]


def test_responses_function_tool_missing_properties_gets_default():
    """object schema 缺 properties 字段 → 补空（Codex 要求）。"""
    req = pa.responses_to_chat_request({
        "model": "m", "input": "hi",
        "tools": [{"type": "function", "name": "f", "parameters": {"type": "object"}}],
    })
    assert req.tools[0]["function"]["parameters"]["properties"] == {}


def test_responses_reasoning_effort_and_max_tokens():
    req = pa.responses_to_chat_request({
        "model": "m", "input": "hi",
        "reasoning": {"effort": "high", "summary": "auto"},
        "max_output_tokens": 4096,
    })
    assert req.reasoning_effort == "high"
    assert req.thinking is True
    assert req.max_tokens == 4096


def test_responses_max_completion_tokens_fallback():
    req = pa.responses_to_chat_request({"model": "m", "input": "hi",
                                        "max_completion_tokens": 512})
    assert req.max_tokens == 512


def test_responses_input_image_multimodal():
    req = pa.responses_to_chat_request({
        "model": "m",
        "input": [{"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "看图"},
            {"type": "input_image", "image_url": "data:image/png;base64,xx"},
        ]}],
    })
    assert req.messages[0].content == "看图"
    assert req.messages[0].content_blocks[0]["type"] == "image_url"
    assert req.messages[0].content_blocks[0]["image_url"]["url"].startswith("data:image/png")


def test_responses_fingerprint_sanitized():
    """Responses 入站同样做竞品指纹净化（硬性规则外圈：上游风控）。"""
    req = pa.responses_to_chat_request({
        "model": "m", "instructions": "You are Claude Code",
        "input": "hi",
    })
    assert "You are Claude Code".lower() not in (req.messages[0].content or "").lower()


# ═══════════════════════ 3. Anthropic 入站映射 ═══════════════════════

def test_anthropic_system_string_and_blocks():
    req = pa.anthropic_to_chat_request({
        "model": "m", "max_tokens": 1024,
        "system": [{"type": "text", "text": "你是助手"},
                   {"type": "text", "text": "x-anthropic-billing-header: 123"}],
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert req.messages[0].role == "system"
    assert req.messages[0].content == "你是助手"  # billing header 剥离


def test_anthropic_tool_use_and_result_roundtrip():
    req = pa.anthropic_to_chat_request({
        "model": "m", "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "查天气"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "我来查"},
                {"type": "tool_use", "id": "toolu_01", "name": "get_weather",
                 "input": {"city": "北京"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": "晴"},
            ]},
        ],
    })
    roles = [m.role for m in req.messages]
    assert roles == ["user", "assistant", "tool"]
    assert req.messages[1].tool_calls[0]["name"] == "get_weather"
    assert req.messages[1].tool_calls[0]["arguments"] == {"city": "北京"}
    assert req.messages[2].tool_call_id == "toolu_01" and req.messages[2].content == "晴"


def test_anthropic_tool_result_error_prefixed():
    req = pa.anthropic_to_chat_request({
        "model": "m", "max_tokens": 1024,
        "messages": [{"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "boom", "is_error": True},
        ]}],
    })
    assert req.messages[0].content.startswith("Error")


def test_anthropic_invalid_tool_id_sanitized():
    """非法 id（含 `:` 等）→ 补齐为合法 [a-zA-Z0-9_-]（9router concerns/toolCall.js）。"""
    req = pa.anthropic_to_chat_request({
        "model": "m", "max_tokens": 1024,
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tool:use:1", "name": "Read", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tool:use:1", "content": "ok"},
            ]},
        ],
    })
    call_id = req.messages[0].tool_calls[0]["id"]
    assert ":" not in call_id and call_id
    # tool_result 的 id 与 tool_use 补齐后保持一致（同一净化口径）
    assert req.messages[1].tool_call_id == call_id


def test_anthropic_thinking_blocks():
    req = pa.anthropic_to_chat_request({
        "model": "m", "max_tokens": 1024,
        "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "想一下"},
                {"type": "text", "text": "答案"},
            ]},
        ],
    })
    assert req.messages[0].reasoning_content == "想一下"
    assert req.messages[0].content == "答案"


def test_anthropic_thinking_disabled_normalized():
    """thinking.type=disabled → thinking=True + effort=none（与 oai_adapter 归一一致）。"""
    req = pa.anthropic_to_chat_request({
        "model": "m", "max_tokens": 1024,
        "thinking": {"type": "disabled"},
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert req.thinking is True and req.reasoning_effort == "none"


def test_anthropic_thinking_enabled_budget_to_effort():
    req = pa.anthropic_to_chat_request({
        "model": "m", "max_tokens": 1024,
        "thinking": {"type": "enabled", "budget_tokens": 8192},
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert req.thinking is True and req.reasoning_effort == "medium"


def test_anthropic_tools_input_schema():
    req = pa.anthropic_to_chat_request({
        "model": "m", "max_tokens": 1024,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "get_weather", "description": "查天气",
                   "input_schema": {"type": "object",
                                    "properties": {"city": {"type": "string"}}}}],
    })
    t = req.tools[0]["function"]
    assert t["name"] == "get_weather"
    assert t["parameters"]["properties"]["city"]["type"] == "string"


def test_anthropic_image_block_to_data_uri():
    req = pa.anthropic_to_chat_request({
        "model": "m", "max_tokens": 1024,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                          "data": "QUJD"}},
        ]}],
    })
    assert req.messages[0].content_blocks[0]["image_url"]["url"] == \
        "data:image/jpeg;base64,QUJD"


def test_anthropic_mid_conversation_system_becomes_user():
    """会话中段 system → user + <instructions> 包裹（9router 同款）。"""
    req = pa.anthropic_to_chat_request({
        "model": "m", "max_tokens": 1024,
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "system", "content": [{"type": "text", "text": "注意安全"}]},
        ],
    })
    assert req.messages[1].role == "user"
    assert "<instructions>" in req.messages[1].content


def test_anthropic_string_content_max_tokens():
    req = pa.anthropic_to_chat_request({
        "model": "m", "max_tokens": 2048,
        "messages": [{"role": "user", "content": "hello"}],
    })
    assert req.max_tokens == 2048
    assert req.messages[0].content == "hello"


# ═══════════════════════ 4. 响应回译 ═══════════════════════

def _chat_request():
    return ChatRequest(model="m", messages=[ChatMessage(role="user", content="x")])


def test_chat_response_to_responses_nonstream():
    resp = ChatResponse(
        content="你好", thinking="想", finish_reason="stop",
        usage=Usage(prompt_tokens=3, completion_tokens=5, total_tokens=8,
                    cached_input_tokens=1, reasoning_tokens=2),
    )
    out = pa.chat_response_to_responses(_chat_request(), resp)
    assert out["object"] == "response" and out["status"] == "completed"
    types = [i["type"] for i in out["output"]]
    assert types == ["reasoning", "message"]
    assert out["output"][1]["content"][0]["text"] == "你好"
    assert out["usage"]["input_tokens"] == 3 and out["usage"]["output_tokens"] == 5
    assert out["usage"]["input_tokens_details"]["cached_tokens"] == 1


def test_chat_response_to_responses_tool_calls_and_length():
    resp = ChatResponse(
        content=None, finish_reason="length",
        tool_calls=[{"id": "call_1", "name": "get_weather", "arguments": {"city": "北京"}}],
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )
    out = pa.chat_response_to_responses(_chat_request(), resp)
    assert out["status"] == "incomplete"
    assert out["incomplete_details"]["reason"] == "max_output_tokens"
    assert out["output"][0]["type"] == "function_call"
    assert json.loads(out["output"][0]["arguments"]) == {"city": "北京"}


def test_chat_response_to_anthropic_nonstream():
    resp = ChatResponse(
        content="答案", thinking="思考", finish_reason="tool_calls",
        tool_calls=[{"id": "t1", "name": "Bash", "arguments": {"command": "ls"}}],
        usage=Usage(prompt_tokens=7, completion_tokens=9, total_tokens=16,
                    cached_input_tokens=2),
    )
    out = pa.chat_response_to_anthropic(_chat_request(), resp)
    assert out["type"] == "message"
    assert [c["type"] for c in out["content"]] == ["thinking", "text", "tool_use"]
    assert out["stop_reason"] == "tool_use"
    assert out["content"][2]["input"] == {"command": "ls"}
    assert out["usage"]["input_tokens"] == 7
    assert out["usage"]["cache_read_input_tokens"] == 2


def test_chat_response_to_anthropic_stop_reason_map():
    for fin, want in (("stop", "end_turn"), ("length", "max_tokens"),
                      ("tool_calls", "tool_use")):
        out = pa.chat_response_to_anthropic(
            _chat_request(),
            ChatResponse(content="x", finish_reason=fin, usage=Usage()))
        assert out["stop_reason"] == want


# ── Responses typed 事件流 ──

_ROLE_FRAME = ('data: {"choices": [{"index": 0, '
               '"delta": {"role": "assistant", "content": ""}}]}\n\n')


def _usage_frame(prompt: int, completion: int) -> str:
    total = prompt + completion
    return ('data: {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],'
            f' "usage": {{"prompt_tokens": {prompt}, "completion_tokens": {completion},'
            f' "total_tokens": {total}}}}}\n\n')


def _sse_events(frames: list[str]) -> list[tuple[str, dict]]:
    """事件字符串列表 → [(event_type, data)]。"""
    out = []
    for chunk in frames:
        for block in chunk.strip().split("\n\n"):
            if not block.strip():
                continue
            etype = None
            data = None
            for line in block.splitlines():
                if line.startswith("event:"):
                    etype = line[6:].strip()
                elif line.startswith("data:"):
                    data = json.loads(line[5:].strip())
            out.append((etype, data))
    return out


@pytest.mark.asyncio
async def test_responses_stream_events_full_flow():
    """内容流：created → item/part/delta → done → completed（output 回填）。"""
    asm = pa.ResponsesStreamAssembler("m")
    out: list[str] = []
    out += asm.feed(_ROLE_FRAME)
    assert out == []  # role 帧无事件
    out += asm.feed('data: {"choices": [{"index": 0, "delta": {"content": "你"}}]}\n\n')
    out += asm.feed('data: {"choices": [{"index": 0, "delta": {"content": "好"}}]}\n\n')
    out += asm.feed(_usage_frame(3, 2))
    out += asm.feed("data: [DONE]\n\n")

    events = _sse_events(out)
    types = [e[0] for e in events]
    assert types[0] == "response.created"
    assert "response.output_item.added" in types
    assert "response.output_text.delta" in types
    assert "response.output_item.done" in types
    assert types[-1] == "response.completed"
    # delta 拼接完整
    deltas = [e[1]["delta"] for e in events if e[0] == "response.output_text.delta"]
    assert "".join(deltas) == "你好"
    # completed.output 回填了 message item（9router #4307）
    completed = events[-1][1]
    assert completed["response"]["output"][0]["content"][0]["text"] == "你好"
    assert completed["response"]["usage"]["input_tokens"] == 3
    # sequence_number 单调递增
    seqs = [e[1]["sequence_number"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)


@pytest.mark.asyncio
async def test_responses_stream_reasoning_then_text():
    asm = pa.ResponsesStreamAssembler("m")
    out = asm.feed('data: {"choices": [{"index": 0, "delta": {"reasoning_content": "想"}}]}\n\n')
    out += asm.feed('data: {"choices": [{"index": 0, "delta": {"content": "答"}}]}\n\n')
    out += asm.feed(_usage_frame(1, 1))
    events = _sse_events(out)
    types = [e[0] for e in events]
    assert "response.reasoning_summary_text.delta" in types
    # reasoning item 先关（done），随后 text item 才 added
    assert types.index("response.reasoning_summary_text.done") < \
        types.index("response.output_text.delta")
    completed = [e for e in events if e[0] == "response.completed"][0][1]
    items = completed["response"]["output"]
    assert [i["type"] for i in items] == ["reasoning", "message"]


@pytest.mark.asyncio
async def test_responses_stream_tool_call():
    """function_call 增量：added → arguments.delta → done；completed.output 含调用。"""
    asm = pa.ResponsesStreamAssembler("m")
    out = asm.feed('data: {"choices": [{"index": 0, "delta": {"tool_calls": '
                   '[{"index": 0, "id": "call_1", "type": "function", '
                   '"function": {"name": "get_weather", "arguments": ""}}]}}]}\n\n')
    out += asm.feed('data: {"choices": [{"index": 0, "delta": {"tool_calls": '
                    '[{"index": 0, "function": {"arguments": "{\\"city\\":\\"北京\\"}"}}]}}]}\n\n')
    out += asm.feed('data: {"choices": [{"index": 0, "delta": {}, '
                    '"finish_reason": "tool_calls"}],'
                    ' "usage": {"prompt_tokens": 2, "completion_tokens": 3,'
                    ' "total_tokens": 5}}\n\n')
    events = _sse_events(out)
    types = [e[0] for e in events]
    assert "response.output_item.added" in types
    assert "response.function_call_arguments.delta" in types
    assert "response.function_call_arguments.done" in types
    completed = [e for e in events if e[0] == "response.completed"][0][1]
    fc = completed["response"]["output"][0]
    assert fc["type"] == "function_call" and fc["name"] == "get_weather"
    assert json.loads(fc["arguments"]) == {"city": "北京"}


@pytest.mark.asyncio
async def test_responses_stream_completes_on_done_without_finish():
    """上游给 usage 但 finish/[DONE] 缺 content 帧：finish() 兜底发 completed。"""
    asm = pa.ResponsesStreamAssembler("m")
    out = asm.feed('data: {"choices": [{"index": 0, "delta": {"content": "hi"}}]}\n\n')
    out += asm.finish()
    events = _sse_events(out)
    assert events[-1][0] == "response.completed"


@pytest.mark.asyncio
async def test_responses_stream_error_frame():
    """上游错误帧 → response.failed + error 事件。"""
    asm = pa.ResponsesStreamAssembler("m")
    out = asm.feed('data: {"error": {"message": "账号额度不足", "code": 402}}\n\n')
    events = _sse_events(out)
    types = [e[0] for e in events]
    assert "response.failed" in types and "error" in types
    failed = [e for e in events if e[0] == "response.failed"][0][1]
    assert "账号额度不足" in failed["response"]["error"]["message"]


@pytest.mark.asyncio
async def test_anthropic_stream_events_full_flow():
    asm = pa.AnthropicStreamAssembler("m")
    out = asm.feed(_ROLE_FRAME)
    assert out == []  # role 帧不产出
    out += asm.feed('data: {"choices": [{"index": 0, "delta": {"content": "你"}}]}\n\n')
    out += asm.feed('data: {"choices": [{"index": 0, "delta": {"content": "好"}}]}\n\n')
    out += asm.feed(_usage_frame(5, 3))
    events = _sse_events(out)
    types = [e[0] for e in events]
    assert types[0] == "message_start"
    assert types[-1] == "message_stop"
    assert "content_block_start" in types and "content_block_stop" in types
    text_deltas = [e[1]["delta"]["text"] for e in events
                   if e[0] == "content_block_delta" and e[1]["delta"]["type"] == "text_delta"]
    assert "".join(text_deltas) == "你好"
    md = [e for e in events if e[0] == "message_delta"][0][1]
    assert md["delta"]["stop_reason"] == "end_turn"
    assert md["usage"]["input_tokens"] == 5 and md["usage"]["output_tokens"] == 3


@pytest.mark.asyncio
async def test_anthropic_stream_reasoning_block():
    asm = pa.AnthropicStreamAssembler("m")
    out = asm.feed('data: {"choices": [{"index": 0, "delta": {"reasoning_content": "想"}}]}\n\n')
    out += asm.feed('data: {"choices": [{"index": 0, "delta": {"content": "答"}}]}\n\n')
    out += asm.finish()
    events = _sse_events(out)
    starts = [e[1]["content_block"]["type"] for e in events
              if e[0] == "content_block_start"]
    assert starts == ["thinking", "text"]
    types = [e[0] for e in events]
    # thinking 块先关再开 text：第一个 block_stop 在第二个 block_start 之前
    second_start = types.index("content_block_start", types.index("content_block_start") + 1)
    assert types.index("content_block_stop") < second_start


@pytest.mark.asyncio
async def test_anthropic_stream_tool_use_events():
    asm = pa.AnthropicStreamAssembler("m")
    out = asm.feed('data: {"choices": [{"index": 0, "delta": {"tool_calls": '
                   '[{"index": 0, "id": "call_1", "function": {"name": "Bash", '
                   '"arguments": "{\\"command\\":\\"ls\\"}"}}]}}]}\n\n')
    out += asm.feed('data: {"choices": [{"index": 0, "delta": {}, '
                    '"finish_reason": "tool_calls"}],'
                    ' "usage": {"prompt_tokens": 1, "completion_tokens": 1,'
                    ' "total_tokens": 2}}\n\n')
    events = _sse_events(out)
    starts = [e[1] for e in events if e[0] == "content_block_start"]
    assert starts[0]["content_block"]["type"] == "tool_use"
    assert starts[0]["content_block"]["name"] == "Bash"
    partials = [e[1]["delta"]["partial_json"] for e in events
                if e[0] == "content_block_delta"
                and (e[1]["delta"] or {}).get("type") == "input_json_delta"]
    assert json.loads("".join(partials)) == {"command": "ls"}
    md = [e for e in events if e[0] == "message_delta"][0][1]
    assert md["delta"]["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_anthropic_stream_error_event():
    asm = pa.AnthropicStreamAssembler("m")
    out = asm.feed('data: {"error": {"message": "boom"}}\n\n')
    events = _sse_events(out)
    assert events[-1][0] == "error"


# ═══════════════════════ 5. 端点矩阵 ═══════════════════════

class _FakeProvider:
    _model_name = "fake"

    def __init__(self, events):
        self._events = events

    async def stream_structured(self, request):
        for e in self._events:
            yield e

    async def chat(self, request):
        from app.models.schemas import ChatResponse, Usage
        return ChatResponse(content="你好", finish_reason="stop",
                            usage=Usage(prompt_tokens=3, completion_tokens=2,
                                        total_tokens=5))


_OK_EVENTS = [
    {"type": "content", "delta": "你好"},
    {"type": "done", "content": "你好", "thinking": None, "tool_calls": [],
     "finish_reason": "stop",
     "usage": Usage(prompt_tokens=3, completion_tokens=2, total_tokens=5),
     "raw_tail": None},
]


def _patch_provider(monkeypatch):
    monkeypatch.setattr(routes_mod, "build_provider",
                        lambda *a, **kw: _FakeProvider(_OK_EVENTS))

    async def _no_sync():
        return []

    monkeypatch.setattr(routes_mod.auth_flow, "sync_models", _no_sync)


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "relay_log_max_rows", 100000)
    settings.relay_api_key = "proto-test-key"
    _run(storage.save_models([_MODEL]))
    _run(storage.set_model_whitelist([]))

    async def _serving_on():
        return True

    monkeypatch.setattr(routes_mod, "_serving", _serving_on)
    _patch_provider(monkeypatch)
    with TestClient(relay_app, raise_server_exceptions=False) as c:
        yield c
    settings.relay_api_key = ""


def test_chat_endpoint_still_works(client):
    """既有 /v1/chat/completions 不回归（OpenAI SSE 直出）。"""
    r = client.post("/v1/chat/completions",
                   json={"model": "glm-x", "stream": True,
                         "messages": [{"role": "user", "content": "hi"}]},
                   headers=_auth())
    assert r.status_code == 200
    assert "data: [DONE]" in r.text
    assert '"choices"' in r.text


def test_responses_endpoint_stream(client):
    """GET /v1/responses：Responses typed 事件流。"""
    r = client.post("/v1/responses",
                   json={"model": "glm-x", "stream": True, "input": "hi"},
                   headers=_auth())
    assert r.status_code == 200
    assert "event: response.created" in r.text
    assert "event: response.output_text.delta" in r.text
    assert "event: response.completed" in r.text
    # 不应泄漏 OpenAI choices 形态
    assert '"choices"' not in r.text


def test_responses_endpoint_nonstream(client):
    r = client.post("/v1/responses",
                   json={"model": "glm-x", "input": "hi"},
                   headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "response" and body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "你好"
    assert body["usage"]["input_tokens"] == 3


def test_messages_endpoint_x_api_key_stream(client):
    """Anthropic 端点：x-api-key 鉴权 + typed 事件流（Claude Code 场景）。"""
    r = client.post("/v1/messages",
                   json={"model": "glm-x", "stream": True, "max_tokens": 1024,
                         "messages": [{"role": "user", "content": "hi"}]},
                   headers=_anthropic_auth())
    assert r.status_code == 200
    assert "event: message_start" in r.text
    assert "event: content_block_delta" in r.text
    assert "event: message_stop" in r.text


def test_messages_endpoint_nonstream(client):
    r = client.post("/v1/messages",
                   json={"model": "glm-x", "max_tokens": 1024,
                         "messages": [{"role": "user", "content": "hi"}]},
                   headers=_anthropic_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["type"] == "message" and body["stop_reason"] == "end_turn"
    assert body["content"][0]["text"] == "你好"
    assert body["usage"]["input_tokens"] == 3


def test_messages_endpoint_bearer_also_accepted(client):
    """Anthropic 端点兼容 Bearer（两种头任一生效）。"""
    r = client.post("/v1/messages",
                   json={"model": "glm-x", "max_tokens": 16,
                         "messages": [{"role": "user", "content": "hi"}]},
                   headers=_auth())
    assert r.status_code == 200


def test_count_tokens_endpoint(client):
    r = client.post("/v1/messages/count_tokens",
                   json={"model": "glm-x",
                         "messages": [{"role": "user", "content": "hello world"}]},
                   headers=_anthropic_auth())
    assert r.status_code == 200
    assert r.json()["input_tokens"] >= 1


def test_endpoints_require_api_key(client):
    """三协议端点全部 401（无鉴权头）。"""
    for path, body in (("/v1/chat/completions", {"model": "m", "messages": []}),
                       ("/v1/responses", {"model": "m", "input": "hi"}),
                       ("/v1/messages", {"model": "m", "messages": []}),
                       ("/v1/messages/count_tokens", {"model": "m", "messages": []})):
        assert client.post(path, json=body).status_code == 401, path


def test_new_endpoints_gated_by_service_switch(tmp_path, monkeypatch):
    """M9 服务开关同样门控新端点：未登录 → 503（含 count_tokens 本地端点）。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    settings.relay_api_key = "proto-test-key"

    async def _not_serving():
        return False

    monkeypatch.setattr(routes_mod, "_serving", _not_serving)
    with TestClient(relay_app, raise_server_exceptions=False) as c:
        for path, body in (("/v1/responses", {"model": "m", "input": "hi"}),
                           ("/v1/messages", {"model": "m", "messages": []}),
                           ("/v1/messages/count_tokens", {"model": "m", "messages": []})):
            r = c.post(path, json=body, headers=_auth())
            assert r.status_code == 503, path
    settings.relay_api_key = ""


def test_wrong_key_401(client):
    r = client.post("/v1/messages", json={"model": "m", "messages": []},
                   headers={"x-api-key": "wrong"})
    assert r.status_code == 401


def test_anthropic_empty_error_body_shape(client, monkeypatch):
    """Anthropic 端点空响应耗尽 → 502 + Anthropic 风格错误体。"""
    class _EmptyProvider:
        _model_name = "fake"

        def __init__(self):
            self.calls = 0

        async def stream_structured(self, request):
            self.calls += 1
            yield {"type": "done", "content": None, "thinking": None, "tool_calls": [],
                   "finish_reason": "stop", "usage": Usage(),
                   "raw_tail": ['data: {"type":"message_stop"}']}

    from relay import oai_adapter
    monkeypatch.setattr(settings, "empty_stream_retries", 0)
    monkeypatch.setattr(routes_mod, "build_provider", lambda *a, **kw: _EmptyProvider())
    r = client.post("/v1/messages",
                   json={"model": "glm-x", "stream": True, "max_tokens": 16,
                         "messages": [{"role": "user", "content": "hi"}]},
                   headers=_anthropic_auth())
    assert r.status_code == 502
    body = r.json()
    assert body["type"] == "error"
    assert "上游返回空响应" in body["error"]["message"]
    assert "message_stop" not in r.text  # 硬性规则 2：不泄漏上游内部行


def test_responses_upstream_error_shape(client, monkeypatch):
    """Responses 端点上游 403 → 403 + OpenAI 风格错误体（Responses 客户端吃该形状）。"""
    async def _raise_iter(model_name, chat_request, include_usage, ctx,
                          usage_collector=None, *, probe=False):
        raise RuntimeError('模型请求失败 403：{"error":"违规内容"}')
        yield  # pragma: no cover

    monkeypatch.setattr(routes_mod, "_sse_with_retry", _raise_iter)
    r = client.post("/v1/responses",
                   json={"model": "glm-x", "stream": True, "input": "hi"},
                   headers=_auth())
    assert r.status_code == 403
    assert r.json()["error"]["code"] == 403


def test_anthropic_upstream_error_shape(client, monkeypatch):
    """Anthropic 端点上游 429 → 429 + Anthropic 错误体（type:error/error.type）。"""
    async def _raise_iter(model_name, chat_request, include_usage, ctx,
                          usage_collector=None, *, probe=False):
        raise RuntimeError('模型请求失败 429：{"error":"rate limited"}')
        yield  # pragma: no cover

    monkeypatch.setattr(routes_mod, "_sse_with_retry", _raise_iter)
    r = client.post("/v1/messages",
                   json={"model": "glm-x", "stream": True, "max_tokens": 16,
                         "messages": [{"role": "user", "content": "hi"}]},
                   headers=_anthropic_auth())
    assert r.status_code == 429
    body = r.json()
    assert body["type"] == "error" and body["error"]["type"] == "upstream_error"


def test_chat_request_log_has_protocol(client):
    """入站协议落库 detail，供日志页/统计区分（Responses 端点 → protocol=openai-responses）。"""
    from relay import db
    r = client.post("/v1/responses",
                   json={"model": "glm-x", "input": "hi"},
                   headers=_auth())
    assert r.status_code == 200
    reqs, _ = _run(db.query_logs(kind="chat_request"))
    assert reqs[0]["detail"]["protocol"] == "openai-responses"


def test_cursor_malformed_case_chat_endpoint(client):
    """Cursor CLI 畸形 case：Responses body（input[]）打到 /v1/chat/completions →
    按 Responses 解析（否则 400），**响应仍回 OpenAI Chat 形状**（客户端吃的是 chat 端点）。"""
    r = client.post("/v1/chat/completions",
                   json={"model": "glm-x", "stream": True, "input": "hi"},
                   headers=_auth())
    assert r.status_code == 200
    assert '"choices"' in r.text  # 回 Chat 形状，不是 Responses typed 事件
    assert "event: response.created" not in r.text
    # 解析侧确实按 Responses 生效：input 字符串被拆成 user 消息后能命中假 provider
    from relay import db
    r2 = client.post("/v1/chat/completions",
                    json={"model": "glm-x", "input": "hi"}, headers=_auth())
    assert r2.status_code == 200
    reqs, _ = _run(db.query_logs(kind="chat_request"))
    assert reqs[0]["detail"]["protocol"] == "openai"  # 回译协议按端点（chat）
