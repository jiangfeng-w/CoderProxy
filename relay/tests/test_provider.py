"""Ta3Provider keyless 单测：请求头伪装 / 工具名双向转换（无网络）。"""
from app.models.providers.ta3 import Ta3Provider
from app.models.schemas import ChatMessage, ChatRequest


def _provider(**kw) -> Ta3Provider:
    base = {"api_key": "llm-abc123", "base_url": "https://lc.yinhaiyun.com/newcoder",
            "model": "glm-5.3-flash"}
    base.update(kw)
    return Ta3Provider(**base)


def test_base_headers_openai():
    p = _provider()
    h = p._base_headers()
    assert h["Authorization"] == "Bearer llm-abc123"
    assert h["api-key"] == "llm-abc123"
    assert h["X-Call-Source"] == "APP"
    assert "Electron" in h["User-Agent"]


def test_base_headers_anthropic():
    p = _provider(model="kimi-k3", meta={"anthropic": True})
    h = p._base_headers()
    assert h["x-api-key"] == "llm-abc123"
    assert h["anthropic-version"] == "2023-06-01"
    assert "Authorization" not in h


def test_build_openai_body_temperature_default():
    p = _provider()
    req = ChatRequest(model="glm-5.3-flash", messages=[ChatMessage(role="user", content="hi")])
    body = p._build_openai_body(req, disguised=[])
    assert body["temperature"] == 0.1  # 对齐参考项目默认
    assert body["stream"] is True
    assert "tools" not in body


def test_restore_tool_calls_mapping():
    """入站：ta3 名 → 真实执行名 + 参数还原。"""
    p = _provider()
    out = p._restore_tool_calls([
        {"id": "c1", "name": "Bash", "arguments": '{"command": "echo hi"}'},
    ])
    assert out[0]["name"] == "terminal_exec"
    assert out[0]["arguments"]["command"] == "echo hi"


def test_disguise_message_unknown_tool_to_text():
    """历史里未映射的工具调用 → 降级为纯文本（避免协议断裂）。"""
    p = _provider()
    out = p._disguise_message(ChatMessage(
        role="assistant",
        content="",
        tool_calls=[{"id": "c1", "name": "some_unknown_tool", "arguments": {}}],
    ))
    assert "tool_calls" not in out
    assert "当前环境不可用" in (out["content"] or "")
