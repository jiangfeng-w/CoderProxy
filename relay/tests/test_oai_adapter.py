"""oai_adapter 纯函数单测：OpenAI 请求解析 / 模型条目转 OpenAI 格式（keyless）。"""
from app.models.schemas import ChatRequest

from relay.oai_adapter import model_to_openai, oai_request_to_chat_request


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
