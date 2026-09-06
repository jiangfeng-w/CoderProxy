"""oai_adapter 纯函数单测：OpenAI 请求解析 / 模型条目转 OpenAI 格式（keyless）。"""
from app.models.schemas import ChatRequest

from relay.oai_adapter import (
    _finish_reason_to_openai,
    _sanitize_fingerprint,
    model_to_openai,
    oai_request_to_chat_request,
)


def test_parse_basic():
    body = {
        "model": "glm-5.3-flash",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": False,
    }
    req = oai_request_to_chat_request(body)
    assert isinstance(req, ChatRequest)
    assert req.model == "glm-5.3-flash"
    assert req.messages[0].content == "hi"
    assert req.messages[0].role == "user"
    assert req.stream is False


def test_sanitize_fingerprint_breaks_identity_sentence():
    # 上游风控指纹：身份句里的品牌词被插入零宽空格，精确子串不再命中
    for line in (
        "You are ZCode, an interactive coding agent",
        "You are an interactive ZCode agent that helps users with software engineering tasks.",
        "You are Claude Code",
        "you are zcode, an interactive coding agent",  # 大小写不敏感
    ):
        out = _sanitize_fingerprint(line)
        assert "​" in out
        # 净化后原文的精确子串（大小写不敏感）不再命中
        assert line.lower() not in out.lower()
        # 去掉零宽空格后应能还原原文（语义不丢）
        assert out.replace("​", "") == line


def test_sanitize_fingerprint_leaves_normal_text():
    # 非身份句里的正常提及不动，避免污染代码/文件内容
    for text in (
        "ZCode 是智谱的产品",
        "帮我打开 Claude Code 的配置",
        "# ZCode Desktop Context",
        None,
        "",
    ):
        assert _sanitize_fingerprint(text) == text


def test_request_messages_sanitized():
    body = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "You are ZCode, an interactive coding agent"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": None, "reasoning_content": "I am You are Claude Code"},
        ],
    }
    req = oai_request_to_chat_request(body)
    assert req.messages[0].content == "You are Z​Code, an interactive coding agent"
    assert req.messages[1].content == "你好"
    assert req.messages[2].reasoning_content == "I am You are C​laude Code"


def test_parse_content_blocks():
    body = {
        "model": "m",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
            ],
        }],
    }
    req = oai_request_to_chat_request(body)
    msg = req.messages[0]
    assert msg.content == "看图"
    assert msg.content_blocks[0]["type"] == "image_url"


def test_parse_tool_calls_history():
    body = {
        "model": "m",
        "messages": [{
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "bash", "arguments": '{"command":"echo hi"}'},
            }],
        }],
    }
    req = oai_request_to_chat_request(body)
    tc = req.messages[0].tool_calls[0]
    assert tc["name"] == "bash"
    assert tc["arguments"] == {"command": "echo hi"}


def test_parse_reasoning_effort_implies_thinking():
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}], "reasoning_effort": "high"}
    req = oai_request_to_chat_request(body)
    assert req.reasoning_effort == "high"
    assert req.thinking is True


def test_parse_thinking_dict_glm_style():
    """GLM/智谱系（ZCode）发 {"type": "enabled"}，不得在校验阶段炸 500。"""
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "x"}],
        "thinking": {"type": "enabled", "budget_tokens": 4096},
    }
    req = oai_request_to_chat_request(body)
    assert req.thinking is True


def test_parse_thinking_dict_disabled():
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "x"}],
        "thinking": {"type": "disabled"},
    }
    req = oai_request_to_chat_request(body)
    assert req.thinking is False


def test_parse_thinking_str_and_bool():
    base = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    assert oai_request_to_chat_request({**base, "thinking": "enabled"}).thinking is True
    assert oai_request_to_chat_request({**base, "thinking": "disabled"}).thinking is False
    assert oai_request_to_chat_request({**base, "thinking": True}).thinking is True
    assert oai_request_to_chat_request({**base, "thinking": "auto"}).thinking is None


def test_finish_reason_anthropic_mapped():
    assert _finish_reason_to_openai("end_turn") == "stop"
    assert _finish_reason_to_openai("tool_use") == "tool_calls"
    assert _finish_reason_to_openai("max_tokens") == "length"
    assert _finish_reason_to_openai("stop_sequence") == "stop"
    # OpenAI 原生值与 None 不受影响
    assert _finish_reason_to_openai("stop") == "stop"
    assert _finish_reason_to_openai("tool_calls") == "tool_calls"
    assert _finish_reason_to_openai(None) is None


def test_model_to_openai():
    m = model_to_openai({
        "name": "glm-5.3-flash", "title": "GLM-5.3-Flash", "context_window": 200000,
        "is_multimodal": True, "anthropic": False,
    })
    assert m["id"] == "glm-5.3-flash"
    assert m["object"] == "model"
    assert m["name"] == "GLM-5.3-Flash"
    assert m["context_window"] == 200000
    assert m["is_multimodal"] is True
