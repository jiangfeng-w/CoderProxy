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


# ───────── 指纹清单补齐（2026-10-16 对齐参考实现 sanitize.go 实测清单） ─────────

def test_sanitize_bare_11128_rewritten():
    """裸 11128 是上游整单拦截条件（与该数字上下文无关）→ 连字符改写保留可读性。"""
    assert _sanitize_fingerprint("上游报错 code=11128") == "上游报错 code=11-128"
    assert _sanitize_fingerprint("错误码 11128 是什么") == "错误码 11-128 是什么"
    # 相邻错误码（11148/11101/11115）不受牵连（留有边界）
    assert _sanitize_fingerprint("code=11148") == "code=11148"
    assert _sanitize_fingerprint("code=11101") == "code=11101"


def test_sanitize_injection_and_feedback_sentences_rewritten():
    """注入指令句 / Codex 首句 / Anthropic 反馈句：一词之差改写，语义不变。"""
    assert _sanitize_fingerprint(
        "Main branch (you will usually use this for PRs)"
    ) == "Default branch (you will usually use this for PRs)"
    assert _sanitize_fingerprint(
        "You are a coding agent running in the Codex CLI, a terminal-based coding assistant."
    ) == ("You are a coding agent running in the Codex CLI tool, "
          "a terminal-based coding assistant.")
    out = _sanitize_fingerprint(
        "To give feedback, users should report the issue at "
        "https://github.com/anthropics/claude-code/issues")
    assert out.startswith("To provide feedback, users should report the issue at ")


def test_sanitize_claude_code_identity_suffix_rewritten():
    """Claude Code 身份句后缀改写（同时受 ZWSP 层保护，两种形态都破坏精确匹配）。"""
    out = _sanitize_fingerprint(
        "You are Claude Code, Anthropic's official CLI for Claude")
    assert "official CLI for Claude" not in out
    assert "official CLI tool for Claude" in out


def test_request_tool_call_arguments_sanitized():
    """工具参数（字符串化 JSON）与 content 同属请求体文本：同样净化。"""
    body = {
        "model": "m",
        "messages": [{
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "Write",
                "arguments": '{"cmd": "echo 11128", "file": "a.txt"}',
            }}],
        }],
    }
    req = oai_request_to_chat_request(body)
    args = req.messages[0].tool_calls[0]["arguments"]
    assert args == {"cmd": "echo 11-128", "file": "a.txt"}


def test_multimodal_text_part_sanitized():
    """多模态数组的 text part 同样过净化（image part 不动）。"""
    body = {"model": "m", "messages": [{"role": "user", "content": [
        {"type": "text", "text": "帮我查 code=11128"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xxx"}},
    ]}]}
    req = oai_request_to_chat_request(body)
    assert req.messages[0].content == "帮我查 code=11-128"


def test_parse_max_completion_tokens_fallback():
    """max_tokens 优先；缺失回退 max_completion_tokens（新字段翻译）。"""
    req = oai_request_to_chat_request({
        "model": "m", "messages": [{"role": "user", "content": "hi"}],
        "max_completion_tokens": 64})
    assert req.max_tokens == 64
    req2 = oai_request_to_chat_request({
        "model": "m", "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 8, "max_completion_tokens": 64})
    assert req2.max_tokens == 8


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


# ── BUG-005 扩展：三协议统一提取器（chat 路径的信号面补齐，对齐 9router） ──


def test_parse_output_config_effort_in_chat_path():
    """混血形状：chat 入站也发 output_config.effort（9router extractThinking 承认）→ 档位保留。"""
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}],
            "output_config": {"effort": "max"}}
    req = oai_request_to_chat_request(body)
    assert req.thinking is True and req.reasoning_effort == "max"


def test_parse_reasoning_object_effort_in_chat_path():
    """reasoning.effort（Responses 形状）在 chat 路径同样识别。"""
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}],
            "reasoning": {"effort": "high"}}
    req = oai_request_to_chat_request(body)
    assert req.thinking is True and req.reasoning_effort == "high"


def test_parse_enable_thinking_with_budget():
    """Qwen 形状扩展：enable_thinking=True + thinking_budget → 开 + budget 反推档位。"""
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}],
            "enable_thinking": True, "thinking_budget": 32000}
    req = oai_request_to_chat_request(body)
    assert req.thinking is True and req.reasoning_effort == "xhigh"


def test_parse_reasoning_effort_off_alias_normalized():
    """顶部 reasoning_effort 的 off/disabled 别名归一为「关」（none）。"""
    for value in ("off", "disabled"):
        req = oai_request_to_chat_request(
            {"model": "m", "messages": [{"role": "user", "content": "x"}],
             "reasoning_effort": value})
        assert req.thinking is True and req.reasoning_effort == "none", value


def test_parse_thinking_adaptive_is_on():
    """thinking.type=adaptive（Claude 新形状）在 chat 路径同样视为开思考。"""
    req = oai_request_to_chat_request(
        {"model": "m", "messages": [{"role": "user", "content": "x"}],
         "thinking": {"type": "adaptive"}})
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
    # 显式关思考归一为 thinking=True + effort="none"（ta3.py 仅此组合真发 disabled）
    assert req.thinking is True
    assert req.reasoning_effort == "none"


def test_parse_thinking_bool_false_means_explicit_off():
    body = {"model": "m", "messages": [{"role": "user", "content": "x"}], "thinking": False}
    req = oai_request_to_chat_request(body)
    assert req.thinking is True
    assert req.reasoning_effort == "none"


def test_parse_enable_thinking_bool():
    """Qwen/阿里系客户端发 enable_thinking 布尔（非标准 OpenAI 字段，但要识别）。"""
    base = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    assert oai_request_to_chat_request({**base, "enable_thinking": True}).thinking is True
    off = oai_request_to_chat_request({**base, "enable_thinking": False})
    assert off.thinking is True  # 显式关归一为 effort=none
    assert off.reasoning_effort == "none"


def test_parse_thinking_str_and_bool():
    base = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    assert oai_request_to_chat_request({**base, "thinking": "enabled"}).thinking is True
    assert oai_request_to_chat_request({**base, "thinking": True}).thinking is True
    assert oai_request_to_chat_request({**base, "thinking": "auto"}).thinking is None
    # 显式关（disabled 字符串）归一为 thinking=True + effort="none"
    off = oai_request_to_chat_request({**base, "thinking": "disabled"})
    assert off.thinking is True
    assert off.reasoning_effort == "none"


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


def test_model_to_openai_reasoning_efforts_from_upstream_thinking_levels():
    # 档位取自上游 completion_options.thinkingLevels（牛码官方配置），含中文标签
    m = model_to_openai({
        "name": "glm-5.3-flash", "anthropic": False,
        "completion_options": {
            "thinkingEnabled": True,
            "thinkingLevels": [
                {"level": "low", "label": "低"},
                {"level": "high", "label": "高"},
                {"level": "max", "label": "极"},
            ],
        },
    })
    assert m["reasoning_efforts"] == ["low", "high", "max"]
    assert m["reasoning_labels"] == {"low": "低", "high": "高", "max": "极"}
    assert m["supports_reasoning"] is True


def test_model_to_openai_thinking_levels_dirty_data():
    # 脏数据防御：非 dict 项 / 缺 level / 重复 level / 缺 label
    m = model_to_openai({
        "name": "m",
        "completion_options": {"thinkingLevels": [
            "bad", {"label": "无level"}, {"level": "low", "label": "低"},
            {"level": "low", "label": "重复"}, {"level": "high"},
        ]},
    })
    assert m["reasoning_efforts"] == ["low", "high"]
    assert m["reasoning_labels"] == {"low": "低"}


def test_model_to_openai_no_reasoning():
    # 无 thinkingLevels 且非 anthropic/thinkingEnabled → 不支持思考（不查内置目录）
    m = model_to_openai({"name": "gemini-3-pro", "anthropic": False})
    assert m["reasoning_efforts"] == []
    assert m["reasoning_labels"] == {}
    assert m["supports_reasoning"] is False


def test_model_to_openai_thinking_enabled_without_levels():
    # thinkingEnabled 但无档位明细 → 支持思考、档位为空（GUI 显示「支持」）
    m = model_to_openai({
        "name": "m", "completion_options": {"thinkingEnabled": True},
    })
    assert m["reasoning_efforts"] == []
    assert m["supports_reasoning"] is True
