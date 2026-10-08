"""多协议入站统一（方案 A：三协议端点全加，主参考 9router）。

支持三种入站协议，全部归一为内部 `ChatRequest`，出站复用 Ta3Provider（零改动）：

| 入站端点 | 协议 | 鉴权头 |
| -- | ---- | ---- |
| `/v1/chat/completions` | OpenAI Chat Completions | `Authorization: Bearer` |
| `/v1/responses` | OpenAI Responses API（Codex CLI） | `Authorization: Bearer` |
| `/v1/messages` | Anthropic Messages（Claude Code） | `x-api-key`（兼容 Bearer） |

本模块只做「识别 + 入站转换 + 响应回译」：
- 识别层 `detect_protocol()`：端点路径 + body 特征双判（9router `detectFormatByEndpoint` 同款，
  含「Cursor CLI 往 chat 端点发 input[]」畸形 case 容忍）；
- 入站转换 `*_to_chat_request()`：对标 9router `request/openai-responses.js` /
  `request/claude-to-openai.js`（含 stripContinuityFields、tool call id 补齐等 concerns）；
- 响应回译：牛码 SSE（OpenAI 风格帧，经 `oai_adapter.stream_openai_sse` 产出）→ Responses
  typed 事件 / Anthropic typed 事件；非流式 JSON 同源组装。对标 9router
  `response/openai-responses.js`、`response/openai-to-claude.js`。

A→B 升级预留：转换层与识别层分离；未来「按 Key 分组 Platform 选协议」只改识别层
（`detect_protocol` 加分支），转换层与出站层一行不改。

设计文档：docs/spec/多协议端点兼容-三协议入站统一/多协议端点兼容-三协议入站统一.md
"""
from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any

from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

from relay.oai_adapter import _parse_thinking, _sanitize_fingerprint

# ─────────────────────────── 协议标识 ───────────────────────────

PROTOCOL_CHAT = "openai"
PROTOCOL_RESPONSES = "openai-responses"
PROTOCOL_ANTHROPIC = "claude"

# Responses item 类型
_RESPONSES_ITEM_MESSAGE = "message"
_RESPONSES_ITEM_FUNCTION_CALL = "function_call"
_RESPONSES_ITEM_CUSTOM_TOOL_CALL = "custom_tool_call"
_RESPONSES_ITEM_FUNCTION_CALL_OUTPUT = "function_call_output"
_RESPONSES_ITEM_CUSTOM_TOOL_CALL_OUTPUT = "custom_tool_call_output"
_RESPONSES_ITEM_ADDITIONAL_TOOLS = "additional_tools"
_RESPONSES_ITEM_REASONING = "reasoning"
_RESPONSES_ITEM_INPUT_TEXT = "input_text"
_RESPONSES_ITEM_OUTPUT_TEXT = "output_text"
_RESPONSES_ITEM_INPUT_IMAGE = "input_image"
_RESPONSES_ITEM_SUMMARY_TEXT = "summary_text"

# Responses 服务端内置工具（无对应 Chat 声明）→ 丢弃（与 sub2api 一致）
_RESPONSES_HOSTED_TOOL_TYPES = {
    "web_search", "web_search_preview", "web_search_call",
    "file_search", "file_search_call",
    "computer", "computer_use_preview", "computer_call",
    "local_shell", "local_shell_call",
    "code_interpreter", "image_generation", "mcp",
}

# Chat finish_reason → Anthropic stop_reason（响应回译）
_ANTHROPIC_FINISH_FROM_CHAT = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "tool_use": "tool_use",
    "content_filter": "end_turn",
}

_SAFE_ID_RE = re.compile(r"[^a-zA-Z0-9_-]")
MAX_RESPONSES_CALL_ID_LEN = 64
_ANTHROPIC_BILLING_HEADER_RE = re.compile(r"^x-anthropic-billing-header:[^\n]*(?:\r?\n)?", re.I)


# ─────────────────────────── 识别层 ───────────────────────────


def detect_protocol(path: str, body: Any) -> str:
    """端点路径 + body 特征双判协议（9router `detectFormatByEndpoint` 同构）。

    - 路径含 `/responses` → Responses；含 `/messages` → Anthropic；
    - 路径含 `/chat/completions`：`input[]` 且无 `messages` 的畸形 case（Cursor CLI
      把 Responses body 打到 chat 端点）容忍按 Responses 解析——该请求体只有
      Responses 解释是有效的；否则按 Chat；
    - 路径未知 → 退回 body 特征判定（`detect_protocol_by_body`），为未来别名留口。
    """
    if not isinstance(path, str):
        path = ""
    if "/responses" in path:
        return PROTOCOL_RESPONSES
    if "/messages" in path:
        return PROTOCOL_ANTHROPIC
    if "/chat/completions" in path:
        if isinstance(body, dict) and not body.get("messages") and body.get("input") is not None:
            return PROTOCOL_RESPONSES
        return PROTOCOL_CHAT
    return detect_protocol_by_body(body)


def detect_protocol_by_body(body: Any) -> str:
    """纯 body 特征判定（端点路径不可用时的兜底）。

    - 有 `input`（array/string）且无 `messages` → Responses；
    - 有 `messages` 且**顶层独立 system** 或消息块含 Anthropic 专属类型
      （tool_use/tool_result/thinking/image）→ Anthropic；
    - 其余 → Chat。
    """
    if not isinstance(body, dict):
        return PROTOCOL_CHAT
    if body.get("messages") is None and body.get("input") is not None:
        return PROTOCOL_RESPONSES
    if body.get("messages") is not None:
        if "system" in body and _looks_anthropic_messages(body.get("messages")):
            return PROTOCOL_ANTHROPIC
        return PROTOCOL_CHAT
    return PROTOCOL_CHAT


def _looks_anthropic_messages(messages: Any) -> bool:
    """messages 里出现 Anthropic 专属块类型 → 判 Anthropic（辅助识别用）。"""
    if not isinstance(messages, list):
        return False
    for m in messages[:8]:
        if not isinstance(m, dict):
            continue
        content = m.get("content")
        if isinstance(content, list):
            for b in content[:8]:
                if isinstance(b, dict) and b.get("type") in (
                        "tool_use", "tool_result", "thinking", "image"):
                    return True
    return False


# ─────────────────────────── concerns 移植（9router） ───────────────────────────


def _sanitize_tool_id(tool_id: str | None) -> str | None:
    """Anthropic tool_use.id 必须是 ^[a-zA-Z0-9_-]+$（concerns/toolCall.js）。"""
    if not tool_id or not isinstance(tool_id, str):
        return None
    sanitized = _SAFE_ID_RE.sub("", tool_id)
    return sanitized or None


def _ensure_tool_call_id(tool_id: str | None, msg_idx: int, tc_idx: int,
                         tool_name: str = "") -> str:
    """工具调用 id 补齐：非法/缺失 → 位置 + 名字生成确定性 id（缓存友好）。

    上游/agent 可能给出带 `:`/`.` 等非法字符的 id（Anthropic 校验拒绝）或完全缺失。
    """
    if isinstance(tool_id, str) and re.fullmatch(r"[a-zA-Z0-9_-]+", tool_id or ""):
        return tool_id
    sanitized = _sanitize_tool_id(tool_id)
    if sanitized:
        return sanitized
    name = _SAFE_ID_RE.sub("", tool_name or "")
    return f"call_msg{msg_idx}_tc{tc_idx}" + (f"_{name}" if name else "")


def _clamp_responses_call_id(call_id: Any) -> str:
    """Responses 上游拒收超长 call_id（>64，9router MAX_RESPONSES_CALL_ID_LEN）。"""
    if not isinstance(call_id, str) or not call_id:
        return f"call_{uuid.uuid4().hex[:24]}"
    return call_id[:MAX_RESPONSES_CALL_ID_LEN]


def _coerce_args_to_str(value: Any) -> str:
    """工具参数 → JSON 字符串（Single-stringify：对象一次序列化，合法 JSON 串原样）。"""
    if value is None or value == "":
        return "{}"
    if isinstance(value, str):
        try:
            json.loads(value)
            return value
        except (json.JSONDecodeError, TypeError):
            return json.dumps({"_raw": value}, ensure_ascii=False)
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return "{}"


def _coerce_output_to_str(value: Any) -> str:
    """function_call_output.output 必须是字符串（Responses 契约）。"""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, list):
        parts: list[str] = []
        for c in value:
            if isinstance(c, dict) and isinstance(c.get("text"), str):
                parts.append(c["text"])
            else:
                try:
                    parts.append(json.dumps(c, ensure_ascii=False))
                except (TypeError, ValueError):
                    parts.append(str(c))
        return "".join(parts)
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _args_to_dict(value: Any) -> dict:
    """工具参数（JSON 串 | dict | 其它）→ dict（解析失败降级 `{"_raw": ...}`）。"""
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value) if value else {}
        except (json.JSONDecodeError, TypeError):
            parsed = {"_raw": value}
        return parsed if isinstance(parsed, dict) else {"_raw": str(parsed)}
    if value is None:
        return {}
    return {"_raw": str(value)}


# ─────────────────────────── 入站：Responses → ChatRequest ───────────────────────────


def _responses_content_to_chat(content: Any) -> tuple[str | None, list[dict] | None]:
    """Responses content[] → (纯文本, Chat content_blocks 附加块)。"""
    if isinstance(content, str):
        return (content or None), None
    if not isinstance(content, list):
        return None, None
    texts: list[str] = []
    blocks: list[dict] = []
    for c in content:
        if not isinstance(c, dict):
            continue
        ctype = c.get("type")
        if ctype in (_RESPONSES_ITEM_INPUT_TEXT, _RESPONSES_ITEM_OUTPUT_TEXT, "text"):
            if c.get("text"):
                texts.append(str(c["text"]))
        elif ctype == _RESPONSES_ITEM_INPUT_IMAGE:
            url = c.get("image_url") or c.get("file_id") or ""
            if url:
                blocks.append({"type": "image_url",
                               "image_url": {"url": url, "detail": c.get("detail") or "auto"}})
        # 其它类型：跳过（不炸）
    return ("\n".join(t for t in texts if t) or None), (blocks or None)


def _responses_tool_to_chat(tool: dict) -> dict | None:
    """Responses tool 声明 → Chat tools；hosted/无名工具丢弃。"""
    if not isinstance(tool, dict):
        return None
    if tool.get("function"):
        return tool  # 已是 Chat 形态
    ttype = str(tool.get("type") or "")
    if ttype in _RESPONSES_HOSTED_TOOL_TYPES:
        return None
    name = tool.get("name")
    if not isinstance(name, str) or not name.strip():
        return None  # hosted（无 name）声明丢弃，不能带 nameless 到上游
    name = name.strip()
    if ttype == "custom":
        return {
            "type": "function",
            "function": {
                "name": name,
                "description": str(tool.get("description") or ""),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "input": {"type": "string",
                                  "description": "Raw freeform input for this custom tool"},
                    },
                    "required": ["input"],
                },
            },
        }
    params = tool.get("parameters")
    if not isinstance(params, dict):
        params = {"type": "object", "properties": {}}
    elif params.get("type") == "object" and not params.get("properties"):
        params = {**params, "properties": {}}
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": str(tool.get("description") or ""),
            "parameters": params,
        },
    }


def responses_to_chat_request(body: dict) -> ChatRequest:
    """OpenAI Responses API 请求体 → ChatRequest（9router openai-responses.js 移植）。

    - `instructions` → 一条 system 消息；
    - `input`（string | array）→ messages[]：message/function_call/function_call_output
      逐条拆解；reasoning item 与 web_search_call 等服务端项跳过；
    - `tools[]`：function/custom → Chat function，hosted → 丢弃；
    - `reasoning.effort` → reasoning_effort；`max_output_tokens` → max_tokens；
    - **continuity 字段不带入**：reasoning item 的 `encrypted_content` 等 store=false
      续传 blob 一律丢弃（9router chatCore.js `stripContinuityFields` 同款，
      否则 Codex 多轮每轮 400）。
    """
    messages: list[ChatMessage] = []

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append(ChatMessage(role="system",
                                    content=_sanitize_fingerprint(instructions)))
    elif isinstance(instructions, list):
        text = "\n".join(
            str(c.get("text") or "") for c in instructions
            if isinstance(c, dict) and c.get("text"))
        if text.strip():
            messages.append(ChatMessage(role="system",
                                        content=_sanitize_fingerprint(text)))

    raw_input = body.get("input")
    extra_tools: list[dict] = []
    if isinstance(raw_input, str):
        raw_input = [{"type": _RESPONSES_ITEM_MESSAGE, "role": "user",
                      "content": [{"type": _RESPONSES_ITEM_INPUT_TEXT, "text": raw_input}]}]
    elif isinstance(raw_input, list) and not raw_input:
        # 空 input[] → 注入占位，避免上游以空 messages 拒绝（9router #389）
        raw_input = [{"type": _RESPONSES_ITEM_MESSAGE, "role": "user",
                      "content": [{"type": _RESPONSES_ITEM_INPUT_TEXT, "text": "..."}]}]
    elif not isinstance(raw_input, list):
        raw_input = []

    pending: list[ChatMessage] = []      # function_call_output 的 tool 消息待冲刷
    pending_reasoning = ""               # reasoning item 文本：挂到下一个 assistant（9router 同款）
    current_assistant: ChatMessage | None = None

    def flush_tool_results() -> None:
        nonlocal pending
        if pending:
            messages.extend(pending)
            pending = []

    def flush_assistant() -> None:
        nonlocal current_assistant, pending_reasoning
        if current_assistant is not None:
            if pending_reasoning and not current_assistant.reasoning_content:
                current_assistant.reasoning_content = _sanitize_fingerprint(pending_reasoning)
                pending_reasoning = ""
            if current_assistant.content or current_assistant.tool_calls:
                messages.append(current_assistant)
            current_assistant = None

    for item in raw_input:
        if not isinstance(item, dict):
            continue
        item_type = item.get("type") or (_RESPONSES_ITEM_MESSAGE if item.get("role") else None)

        if item_type == _RESPONSES_ITEM_MESSAGE:
            flush_assistant()
            flush_tool_results()
            role = str(item.get("role") or "user")
            if role not in ("system", "developer", "user", "assistant", "tool"):
                role = "user"
            text, blocks = _responses_content_to_chat(item.get("content"))
            kwargs: dict = {}
            if role == "assistant" and pending_reasoning:
                # reasoning item 的文本挂到紧随的 assistant（thinking 模型多轮回传要求）
                kwargs["reasoning_content"] = _sanitize_fingerprint(pending_reasoning)
                pending_reasoning = ""
            elif role != "assistant":
                pending_reasoning = ""  # 非 assistant 消息到达：失效（9router 同款）
            messages.append(ChatMessage(
                role=role,
                content=_sanitize_fingerprint(text),
                content_blocks=blocks,
                **kwargs,
            ))

        elif item_type in (_RESPONSES_ITEM_FUNCTION_CALL, _RESPONSES_ITEM_CUSTOM_TOOL_CALL):
            if current_assistant is None:
                current_assistant = ChatMessage(role="assistant", content=None, tool_calls=[])
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                continue  # 无名调用丢弃（上游拒绝，9router #444）
            if item_type == _RESPONSES_ITEM_CUSTOM_TOOL_CALL:
                raw_val = item.get("input")
                args = {"input": raw_val if isinstance(raw_val, str)
                        else json.dumps(raw_val if raw_val is not None else "", ensure_ascii=False)}
            else:
                args = _args_to_dict(item.get("arguments"))
            current_assistant.tool_calls.append({
                "id": _clamp_responses_call_id(item.get("call_id")),
                "name": name.strip(),
                "arguments": args,
            })

        elif item_type in (_RESPONSES_ITEM_FUNCTION_CALL_OUTPUT,
                           _RESPONSES_ITEM_CUSTOM_TOOL_CALL_OUTPUT):
            flush_assistant()
            pending.append(ChatMessage(
                role="tool",
                tool_call_id=_clamp_responses_call_id(item.get("call_id")),
                content=_coerce_output_to_str(item.get("output")),
            ))

        elif item_type == _RESPONSES_ITEM_ADDITIONAL_TOOLS:
            extra = item.get("tools")
            if isinstance(extra, list):
                extra_tools.extend(t for t in extra if isinstance(t, dict))

        elif item_type == _RESPONSES_ITEM_REASONING:
            # 展示/续传用途：文本挂到下一个 assistant（见 flush_assistant）；加密 blob
            # （encrypted_content 等 store=false 续传字段）一律丢弃（stripContinuityFields 同款）
            if isinstance(item.get("summary"), list):
                txt = "\n".join(str(s.get("text") or "") for s in item["summary"]
                                if isinstance(s, dict) and s.get("text"))
            elif isinstance(item.get("content"), list):
                txt = "\n".join(str(c.get("text") or "") for c in item["content"]
                                if isinstance(c, dict) and c.get("text"))
            else:
                txt = ""
            if txt:
                pending_reasoning = (f"{pending_reasoning}\n{txt}"
                                     if pending_reasoning else txt)
            continue
        # 其它未知 item（web_search_call / computer_call / local_shell_call ...）跳过

    flush_assistant()
    flush_tool_results()

    # tools：Responses 声明 → Chat function；hosted/无名丢弃
    tools_out: list[dict] = []
    for t in [*(body.get("tools") or []), *extra_tools]:
        conv = _responses_tool_to_chat(t) if isinstance(t, dict) else None
        if conv is not None:
            tools_out.append(conv)

    thinking = _parse_thinking(body.get("thinking"))
    reasoning_effort = None
    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and isinstance(reasoning.get("effort"), str):
        reasoning_effort = reasoning["effort"]
    if reasoning_effort and thinking is None:
        thinking = True

    max_tokens = body.get("max_output_tokens")
    if max_tokens is None:
        max_tokens = body.get("max_completion_tokens")
    if max_tokens is None:
        max_tokens = body.get("max_tokens")

    return ChatRequest(
        messages=messages,
        model=str(body.get("model") or ""),
        stream=bool(body.get("stream", False)),
        tools=tools_out or None,
        temperature=body.get("temperature"),
        max_tokens=max_tokens,
        reasoning_effort=reasoning_effort,
        thinking=thinking,
    )


# ─────────────────────────── 入站：Anthropic → ChatRequest ───────────────────────────


def _anthropic_content_to_chat(
        content: Any, msg_idx: int,
) -> tuple[str | None, list[dict] | None, list[dict] | None, str | None]:
    """Anthropic content（str | blocks）→ (文本, content_blocks, tool_calls, thinking)。

    - text → 文本；image（base64/url）→ Chat image_url 块；
    - tool_use → tool_calls（id 非法即补齐）；thinking → reasoning_content；
    - tool_result 由调用方转 role=tool 消息（OpenAI tool 角色只吃文本）。
    """
    tool_calls: list[dict] = []
    thinking: str | None = None
    if isinstance(content, str):
        return (content or None), None, None, None
    if not isinstance(content, list):
        return None, None, None, None

    texts: list[str] = []
    blocks: list[dict] = []
    for k, block in enumerate(content):
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            if block.get("text"):
                texts.append(str(block["text"]))
        elif btype == "image":
            src = block.get("source") or {}
            if src.get("type") == "base64" and src.get("data"):
                media = src.get("media_type") or "image/png"
                blocks.append({"type": "image_url",
                               "image_url": {"url": f"data:{media};base64,{src['data']}"}})
            elif isinstance(block.get("url"), str) and block["url"]:
                blocks.append({"type": "image_url",
                               "image_url": {"url": block["url"], "detail": "auto"}})
        elif btype == "tool_use":
            tool_calls.append({
                "id": _ensure_tool_call_id(block.get("id"), msg_idx, k,
                                           str(block.get("name") or "")),
                "name": str(block.get("name") or ""),
                "arguments": _args_to_dict(block.get("input")),
            })
        elif btype == "thinking":
            t = block.get("thinking")
            if isinstance(t, str) and t:
                thinking = f"{thinking}\n{t}" if thinking else t
        # tool_result 由调用方处理

    return (("\n".join(t for t in texts if t) or None), (blocks or None),
            (tool_calls or None), thinking)


def _anthropic_tool_result_to_messages(block: dict, msg_idx: int,
                                       block_idx: int) -> list[ChatMessage]:
    """tool_result 块 → role=tool 消息（数组型结果拼字符串，图片给占位标签）。"""
    content = block.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append(str(c.get("text") or ""))
            elif isinstance(c, dict) and c.get("type") == "image":
                parts.append("[image content omitted]")
            else:
                try:
                    parts.append(json.dumps(c, ensure_ascii=False))
                except (TypeError, ValueError):
                    parts.append(str(c))
        text = "\n".join(p for p in parts if p)
    elif content is None:
        text = ""
    else:
        text = _coerce_output_to_str(content)
    if block.get("is_error") and text and not text.startswith("Error"):
        text = f"Error: {text}"
    return [ChatMessage(
        role="tool",
        tool_call_id=_ensure_tool_call_id(block.get("tool_use_id"), msg_idx, block_idx),
        content=text,
    )]


def anthropic_to_chat_request(body: dict) -> ChatRequest:
    """Anthropic Messages 请求体 → ChatRequest（9router claude-to-openai.js 移植）。

    - 顶层 `system`（string | block 数组）→ system 消息（剥离 billing header）；
    - `messages[]`：user 的 content 块拆 text/image/tool_result；assistant 的 content 块拆
      text/tool_use/thinking；tool_result → role=tool；会话中段 system → user（Anthropic 放置规则）；
    - `tools[].input_schema` → Chat function.parameters；
    - `thinking.type=disabled` → 显式关思考（thinking=True + effort=none，与
      oai_adapter 关思考归一一致）；budget_tokens → 粗粒度档位；
    - tool_use/tool_result id 非法（含 `:` 等）→ 补齐为 `[a-zA-Z0-9_-]` 合法 id。
    """
    messages: list[ChatMessage] = []

    system = body.get("system")
    if system:
        text = _anthropic_system_text(system)
        if text.strip():
            messages.append(ChatMessage(role="system",
                                        content=_sanitize_fingerprint(text)))

    raw_messages = body.get("messages")
    if not isinstance(raw_messages, list):
        raw_messages = []

    for i, m in enumerate(raw_messages):
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "user")
        content = m.get("content")

        if role == "assistant":
            text, blocks, tool_calls, thinking = _anthropic_content_to_chat(content, i)
            kwargs: dict = {}
            if tool_calls:
                kwargs["tool_calls"] = tool_calls
            if thinking:
                kwargs["reasoning_content"] = _sanitize_fingerprint(thinking)
            messages.append(ChatMessage(role="assistant",
                                        content=_sanitize_fingerprint(text),
                                        content_blocks=blocks, **kwargs))
            continue

        if role == "system":
            # 会话中段 system → user + <instructions> 包裹（9router 同款）
            text = _anthropic_system_text(content)
            if text.strip():
                messages.append(ChatMessage(
                    role="user",
                    content=_sanitize_fingerprint(f"<instructions>\n{text}\n</instructions>")))
            continue

        # user（或未知 role 归 user）
        if not isinstance(content, list):
            text = content if isinstance(content, str) else (str(content) if content else "")
            messages.append(ChatMessage(role="user", content=_sanitize_fingerprint(text)))
            continue

        tool_results = [b for b in content
                        if isinstance(b, dict) and b.get("type") == "tool_result"]
        if tool_results:
            for k, tr in enumerate(content):
                if isinstance(tr, dict) and tr.get("type") == "tool_result":
                    messages.extend(_anthropic_tool_result_to_messages(tr, i, k))
        text, blocks, _tc, _th = _anthropic_content_to_chat(content, i)
        if text or blocks:
            messages.append(ChatMessage(role="user", content=_sanitize_fingerprint(text),
                                        content_blocks=blocks))

    # tools：input_schema → parameters
    tools_out: list[dict] = []
    for t in (body.get("tools") or []):
        if not isinstance(t, dict):
            continue
        name = str(t.get("name") or "").strip()
        if not name:
            continue
        schema = t.get("input_schema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        tools_out.append({
            "type": "function",
            "function": {
                "name": name,
                "description": str(t.get("description") or ""),
                "parameters": schema,
            },
        })

    # thinking 归一（与 oai_adapter 关思考归一一致）
    thinking_flag: bool | None = None
    reasoning_effort = body.get("reasoning_effort")
    if isinstance(reasoning_effort, str) and reasoning_effort:
        thinking_flag = True
    thinking_cfg = body.get("thinking")
    if isinstance(thinking_cfg, dict):
        ttype = str(thinking_cfg.get("type") or "")
        if ttype == "disabled":
            thinking_flag = True
            reasoning_effort = "none"
        elif ttype in ("enabled", "adaptive"):
            thinking_flag = True
            if not reasoning_effort:
                reasoning_effort = _budget_to_effort(thinking_cfg.get("budget_tokens"))
    elif isinstance(thinking_cfg, bool):
        thinking_flag = True
        if not thinking_cfg:
            reasoning_effort = "none"

    return ChatRequest(
        messages=messages,
        model=str(body.get("model") or ""),
        stream=bool(body.get("stream", False)),
        tools=tools_out or None,
        temperature=body.get("temperature"),
        max_tokens=body.get("max_tokens"),
        reasoning_effort=reasoning_effort,
        thinking=thinking_flag,
    )


def _budget_to_effort(budget: Any) -> str | None:
    """Anthropic budget_tokens → 档位名（粗粒度；ta3 侧还会按模型归一）。"""
    try:
        b = int(budget)
    except (TypeError, ValueError):
        return None
    if b <= 0:
        return "none"
    if b <= 4096:
        return "low"
    if b <= 16384:
        return "medium"
    return "high"


def _anthropic_system_text(system: Any) -> str:
    """Anthropic system（string | [{type:text,text}]）→ 文本；剥离计费头。"""
    if isinstance(system, str):
        return _ANTHROPIC_BILLING_HEADER_RE.sub("", system)
    if isinstance(system, list):
        parts = []
        for s in system:
            if isinstance(s, dict) and s.get("type") == "text" and s.get("text"):
                parts.append(_ANTHROPIC_BILLING_HEADER_RE.sub("", str(s["text"])))
            elif isinstance(s, str):
                parts.append(_ANTHROPIC_BILLING_HEADER_RE.sub("", s))
        return "\n".join(p for p in parts if p)
    return ""


def request_to_chat_request(protocol: str, body: dict) -> ChatRequest:
    """识别层产出协议 → 对应入站转换器（统一入口，供 routes 调用）。"""
    if protocol == PROTOCOL_RESPONSES:
        return responses_to_chat_request(body)
    if protocol == PROTOCOL_ANTHROPIC:
        return anthropic_to_chat_request(body)
    from relay.oai_adapter import oai_request_to_chat_request
    return oai_request_to_chat_request(body)


# ─────────────────────────── 响应回译：非流式 ───────────────────────────


def _usage_to_responses(usage: Usage) -> dict:
    prompt = usage.prompt_tokens or 0
    completion = usage.completion_tokens or 0
    total = usage.total_tokens or (prompt + completion)
    return {
        "input_tokens": prompt,
        "input_tokens_details": {"cached_tokens": usage.cached_input_tokens or 0},
        "output_tokens": completion,
        "output_tokens_details": {"reasoning_tokens": usage.reasoning_tokens or 0},
        "total_tokens": total,
    }


def chat_response_to_responses(request: ChatRequest, response: ChatResponse) -> dict:
    """ChatResponse → Responses API 非流式 JSON（output[] 类型化数组）。

    - thinking → `{type:"reasoning", summary:[{type:"summary_text"}]}`；
    - content → `{type:"message", role:"assistant", content:[{type:"output_text"}]}`；
    - tool_calls → `{type:"function_call", call_id, name, arguments}`；
    - finish_reason=length → status=incomplete + incomplete_details.reason=max_output_tokens。
    """
    model = request.model or response.model or ""
    resp_id = f"resp_{uuid.uuid4().hex[:24]}"
    output: list[dict] = []
    if response.thinking:
        output.append({
            "id": f"rs_{uuid.uuid4().hex[:16]}",
            "type": _RESPONSES_ITEM_REASONING,
            "summary": [{"type": _RESPONSES_ITEM_SUMMARY_TEXT, "text": response.thinking}],
        })
    if response.content:
        output.append({
            "id": f"msg_{uuid.uuid4().hex[:16]}",
            "type": _RESPONSES_ITEM_MESSAGE,
            "status": "completed",
            "role": "assistant",
            "content": [{"type": _RESPONSES_ITEM_OUTPUT_TEXT,
                         "annotations": [], "logprobs": [], "text": response.content}],
        })
    for tc in response.tool_calls or []:
        output.append({
            "id": f"fc_{uuid.uuid4().hex[:16]}",
            "type": _RESPONSES_ITEM_FUNCTION_CALL,
            "status": "completed",
            "call_id": _clamp_responses_call_id(tc.get("id")),
            "name": str(tc.get("name") or ""),
            "arguments": _coerce_args_to_str(tc.get("arguments")),
        })
    if not output:
        # 零产出理论不可达（空响应防御已在 oai_adapter 拦截并 502）；兜底给空 message
        output.append({
            "id": f"msg_{uuid.uuid4().hex[:16]}",
            "type": _RESPONSES_ITEM_MESSAGE,
            "status": "completed",
            "role": "assistant",
            "content": [{"type": _RESPONSES_ITEM_OUTPUT_TEXT,
                         "annotations": [], "logprobs": [], "text": ""}],
        })

    finish = response.finish_reason or "stop"
    status = "incomplete" if finish == "length" else "completed"
    return {
        "id": resp_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "background": False,
        "error": None,
        "incomplete_details": ({"reason": "max_output_tokens"}
                               if status == "incomplete" else None),
        "model": model,
        "output": output,
        "usage": _usage_to_responses(response.usage),
    }


def chat_response_to_anthropic(request: ChatRequest, response: ChatResponse) -> dict:
    """ChatResponse → Anthropic Messages 非流式 JSON。

    content 顺序：thinking（若有）→ text（若有）→ tool_use[]；
    stop_reason 由 finish_reason 逆映射（stop→end_turn、length→max_tokens、tool_calls→tool_use）。
    """
    model = request.model or response.model or ""
    content: list[dict] = []
    if response.thinking:
        content.append({"type": "thinking", "thinking": response.thinking, "signature": ""})
    if response.content:
        content.append({"type": "text", "text": response.content})
    for i, tc in enumerate(response.tool_calls or []):
        content.append({
            "type": "tool_use",
            "id": _ensure_tool_call_id(tc.get("id"), 0, i, str(tc.get("name") or "")),
            "name": str(tc.get("name") or ""),
            "input": _args_to_dict(tc.get("arguments")),
        })
    if not content:
        content.append({"type": "text", "text": ""})

    usage = response.usage
    out_usage = {
        "input_tokens": usage.prompt_tokens or 0,
        "output_tokens": usage.completion_tokens or 0,
    }
    if usage.cached_input_tokens:
        out_usage["cache_read_input_tokens"] = usage.cached_input_tokens

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": _ANTHROPIC_FINISH_FROM_CHAT.get(response.finish_reason or "stop",
                                                       "end_turn"),
        "stop_sequence": None,
        "usage": out_usage,
    }


# ─────────────────────────── 流式组装公用 ───────────────────────────


def _parse_sse_data(frame: str) -> Any:
    """从 `data: {...}` SSE 帧解析载荷；非 data 行返回 None，[DONE] 原样返回。"""
    for line in str(frame).splitlines():
        line = line.strip()
        if line.startswith("data:"):
            data = line[len("data:"):].strip()
            if data == "[DONE]":
                return "[DONE]"
            try:
                return json.loads(data)
            except (json.JSONDecodeError, TypeError):
                return None
    return None


def _usage_from_openai(usage: dict) -> Usage:
    details = usage.get("prompt_tokens_details") or {}
    out_details = usage.get("completion_tokens_details") or {}
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        cached_input_tokens=int(details.get("cached_tokens") or 0),
        reasoning_tokens=int(out_details.get("reasoning_tokens") or 0),
    )


def make_stream_assembler(protocol: str, model: str):
    """按协议创建流式组装器；Chat 协议无需组装（返回 None）。"""
    if protocol == PROTOCOL_RESPONSES:
        return ResponsesStreamAssembler(model)
    if protocol == PROTOCOL_ANTHROPIC:
        return AnthropicStreamAssembler(model)
    return None


# ─────────────────────────── 流式：Responses typed 事件 ───────────────────────────


class ResponsesStreamAssembler:
    """OpenAI SSE 帧 → Responses typed 事件（9router response/openai-responses.js 移植）。

    输入 = `oai_adapter.stream_openai_sse` 产出的帧（`data: {...}` + `[DONE]`），
    逐帧 `feed()` 返回该帧应输出的事件字符串列表（含 `event:` + `data:` 行）。
    要点：

    - 首个真实产出前不发 `response.created`（空响应防御下首帧必为 role 帧：不产出事件）；
    - reasoning → reasoning summary 事件；text → output_text 事件；tool_calls → function_call
      事件（按 index 管线化，arguments 增量直发；added 前缓存的参数在 added 时补发）；
    - finish 帧只关 item；`response.completed` 等 usage 到达（或流结束）再发——
      usage 不能冻结在占位 0（9router #3432：Responses 客户端靠 usage 做上下文计量）；
    - `response.completed.output` 回填全部 item（9router #4307：只读终态事件的客户端
      否则视为空回合）；
    - 上游错误帧 → `response.failed` + `error` 事件。
    """

    def __init__(self, model: str, *, response_id: str | None = None):
        self.model = model
        self.response_id = response_id or f"resp_{uuid.uuid4().hex[:24]}"
        self.created = int(time.time())
        self.seq = 0
        self.started = False
        self.completed = False
        self.failed = False

        self.reasoning_id = ""
        self.reasoning_open = False
        self.reasoning_closed = False
        self.reasoning_buf = ""
        self._reasoning_index: int | None = None

        self.text_open = False
        self.text_closed = False
        self.text_buf = ""
        self._text_index: int | None = None

        self.tool_calls: dict[int, dict] = {}
        self.finish_reason: str | None = None
        self.usage: Usage | None = None
        self.completed_items: dict[int, dict] = {}
        self._next_output_index = 0

    # ── 事件封装 ──

    def _emit(self, event_type: str, data: dict) -> str:
        self.seq += 1
        data = {**data, "sequence_number": self.seq}
        return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def _ensure_started(self) -> list[str]:
        if self.started:
            return []
        self.started = True
        base = {"id": self.response_id, "object": "response",
                "created_at": self.created, "status": "in_progress"}
        return [
            self._emit("response.created",
                       {"type": "response.created",
                        "response": {**base, "background": False, "error": None, "output": []}}),
            self._emit("response.in_progress",
                       {"type": "response.in_progress", "response": base}),
        ]

    # ── reasoning ──

    def _reasoning_delta(self, delta: str) -> list[str]:
        if not delta:
            return []
        out = self._ensure_started()
        if not self.reasoning_open:
            if self._reasoning_index is None:
                self._reasoning_index = self._next_output_index
                self._next_output_index += 1
            self.reasoning_open = True
            self.reasoning_id = f"rs_{self.response_id}_{self._reasoning_index}"
            out.append(self._emit("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": self._reasoning_index,
                "item": {"id": self.reasoning_id, "type": _RESPONSES_ITEM_REASONING,
                         "summary": []},
            }))
            out.append(self._emit("response.reasoning_summary_part.added", {
                "type": "response.reasoning_summary_part.added",
                "item_id": self.reasoning_id,
                "output_index": self._reasoning_index,
                "summary_index": 0,
                "part": {"type": _RESPONSES_ITEM_SUMMARY_TEXT, "text": ""},
            }))
        self.reasoning_buf += delta
        out.append(self._emit("response.reasoning_summary_text.delta", {
            "type": "response.reasoning_summary_text.delta",
            "item_id": self.reasoning_id,
            "output_index": self._reasoning_index,
            "summary_index": 0,
            "delta": delta,
        }))
        return out

    def _close_reasoning(self) -> list[str]:
        if not self.reasoning_open or self.reasoning_closed:
            return []
        self.reasoning_closed = True
        item = {"id": self.reasoning_id, "type": _RESPONSES_ITEM_REASONING,
                "summary": [{"type": _RESPONSES_ITEM_SUMMARY_TEXT, "text": self.reasoning_buf}]}
        out = [
            self._emit("response.reasoning_summary_text.done", {
                "type": "response.reasoning_summary_text.done",
                "item_id": self.reasoning_id,
                "output_index": self._reasoning_index,
                "summary_index": 0,
                "text": self.reasoning_buf,
            }),
            self._emit("response.reasoning_summary_part.done", {
                "type": "response.reasoning_summary_part.done",
                "item_id": self.reasoning_id,
                "output_index": self._reasoning_index,
                "summary_index": 0,
                "part": {"type": _RESPONSES_ITEM_SUMMARY_TEXT, "text": self.reasoning_buf},
            }),
            self._emit("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": self._reasoning_index,
                "item": item,
            }),
        ]
        self.completed_items[self._reasoning_index] = item
        return out

    # ── text ──

    def _text_delta(self, delta: str) -> list[str]:
        if not delta:
            return []
        out = self._ensure_started()
        out.extend(self._close_reasoning())
        if self._text_index is None:
            self._text_index = self._next_output_index
            self._next_output_index += 1
        msg_id = f"msg_{self.response_id}_{self._text_index}"
        if not self.text_open:
            self.text_open = True
            out.append(self._emit("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": self._text_index,
                "item": {"id": msg_id, "type": _RESPONSES_ITEM_MESSAGE,
                         "content": [], "role": "assistant"},
            }))
            out.append(self._emit("response.content_part.added", {
                "type": "response.content_part.added",
                "item_id": msg_id,
                "output_index": self._text_index,
                "content_index": 0,
                "part": {"type": _RESPONSES_ITEM_OUTPUT_TEXT,
                         "annotations": [], "logprobs": [], "text": ""},
            }))
        self.text_buf += delta
        out.append(self._emit("response.output_text.delta", {
            "type": "response.output_text.delta",
            "item_id": msg_id,
            "output_index": self._text_index,
            "content_index": 0,
            "delta": delta,
            "logprobs": [],
        }))
        return out

    def _close_text(self) -> list[str]:
        if not self.text_open or self.text_closed:
            return []
        self.text_closed = True
        idx = self._text_index if self._text_index is not None else 0
        msg_id = f"msg_{self.response_id}_{idx}"
        item = {"id": msg_id, "type": _RESPONSES_ITEM_MESSAGE, "status": "completed",
                "role": "assistant",
                "content": [{"type": _RESPONSES_ITEM_OUTPUT_TEXT, "annotations": [],
                             "logprobs": [], "text": self.text_buf}]}
        out = [
            self._emit("response.output_text.done", {
                "type": "response.output_text.done",
                "item_id": msg_id,
                "output_index": idx,
                "content_index": 0,
                "text": self.text_buf,
                "logprobs": [],
            }),
            self._emit("response.content_part.done", {
                "type": "response.content_part.done",
                "item_id": msg_id,
                "output_index": idx,
                "content_index": 0,
                "part": {"type": _RESPONSES_ITEM_OUTPUT_TEXT, "annotations": [],
                         "logprobs": [], "text": self.text_buf},
            }),
            self._emit("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": idx,
                "item": item,
            }),
        ]
        self.completed_items[idx] = item
        return out

    # ── tool calls ──

    def _tool_delta(self, tc: dict) -> list[str]:
        try:
            idx = int(tc.get("index", 0))
        except (TypeError, ValueError):
            idx = 0
        slot = self.tool_calls.setdefault(
            idx, {"id": "", "name": "", "args": "", "added": False, "done": False,
                  "output_index": None})
        if tc.get("id"):
            slot["id"] = str(tc["id"])
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["name"] = str(fn["name"])

        out = self._ensure_started()
        out.extend(self._close_reasoning())
        out.extend(self._close_text())

        newly_added = False
        if not slot["added"] and slot["id"] and slot["name"]:
            slot["added"] = True
            slot["output_index"] = self._next_output_index
            self._next_output_index += 1
            newly_added = True
            out.append(self._emit("response.output_item.added", {
                "type": "response.output_item.added",
                "output_index": slot["output_index"],
                "item": {"id": f"fc_{slot['id']}", "type": _RESPONSES_ITEM_FUNCTION_CALL,
                         "arguments": "", "call_id": slot["id"], "name": slot["name"]},
            }))
            if slot["args"]:
                # added 前缓存的参数补发为一次 delta，Delta-only 客户端不丢参数
                out.append(self._emit("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "item_id": f"fc_{slot['id']}",
                    "output_index": slot["output_index"],
                    "delta": slot["args"],
                }))

        args_delta = fn.get("arguments")
        if args_delta is not None and args_delta != "":
            args_delta = str(args_delta)
            slot["args"] += args_delta
            if slot["added"]:
                out.append(self._emit("response.function_call_arguments.delta", {
                    "type": "response.function_call_arguments.delta",
                    "item_id": f"fc_{slot['id']}",
                    "output_index": slot["output_index"],
                    "delta": args_delta,
                }))
            # added 前先缓存（id/name 未齐），added 时统一补发一次
        return out

    def _close_tools(self) -> list[str]:
        out: list[str] = []
        for idx in sorted(self.tool_calls.keys()):
            slot = self.tool_calls[idx]
            if not slot["added"] or slot["done"]:
                continue
            slot["done"] = True
            args = slot["args"] or "{}"
            try:
                json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = json.dumps({"_raw": args}, ensure_ascii=False)
            item = {"id": f"fc_{slot['id']}", "type": _RESPONSES_ITEM_FUNCTION_CALL,
                    "status": "completed", "arguments": args,
                    "call_id": slot["id"], "name": slot["name"]}
            out.append(self._emit("response.function_call_arguments.done", {
                "type": "response.function_call_arguments.done",
                "item_id": f"fc_{slot['id']}",
                "output_index": slot["output_index"],
                "arguments": args,
            }))
            out.append(self._emit("response.output_item.done", {
                "type": "response.output_item.done",
                "output_index": slot["output_index"],
                "item": item,
            }))
            self.completed_items[slot["output_index"]] = item
        return out

    # ── 完成 / 失败 ──

    def _send_completed(self, status: str = "completed") -> list[str]:
        if self.completed or self.failed:
            return []
        self.completed = True
        output = [self.completed_items[k] for k in sorted(self.completed_items.keys())]
        response: dict = {
            "id": self.response_id,
            "object": "response",
            "created_at": self.created,
            "status": status,
            "background": False,
            "error": None,
            "output": output,
        }
        if self.usage is not None:
            mapped = _usage_to_responses(self.usage)
            if (mapped["input_tokens"] + mapped["output_tokens"]) > 0:
                response["usage"] = mapped
        if status == "incomplete":
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        return [self._emit("response.completed",
                           {"type": "response.completed", "response": response})]

    def failed_event(self, message: str, status: Any = None) -> list[str]:
        """上游中途失败 → `response.failed` + `error` 事件（SSE 头已发时的收尾）。"""
        if self.failed or self.completed:
            return []
        self.failed = True
        out = self._ensure_started()
        err = {"code": str(status or "upstream_error"), "message": str(message)[:1000]}
        out.append(self._emit("response.failed", {
            "type": "response.failed",
            "response": {"id": self.response_id, "object": "response",
                         "created_at": self.created, "status": "failed",
                         "error": err, "output": []},
        }))
        out.append(self._emit("error", {"type": "error", "code": err["code"],
                                        "message": err["message"]}))
        return out

    # ── 主入口 ──

    def feed(self, sse_frame: str) -> list[str]:
        """吃一个 OpenAI SSE 帧，产出该帧对应的 Responses 事件。"""
        payload = _parse_sse_data(sse_frame)
        if payload is None:
            return []
        if payload == "[DONE]":
            out = self._close_reasoning() + self._close_text() + self._close_tools()
            out.extend(self._send_completed(self._complete_status()))
            return out
        if not isinstance(payload, dict):
            return []
        if payload.get("error"):
            err = payload["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            return self.failed_event(str(msg or "upstream error"), code)

        choices = payload.get("choices") or []
        usage = payload.get("usage")
        if usage and not choices:
            self.usage = _usage_from_openai(usage)
            if self.finish_reason:
                return self._send_completed(self._complete_status())
            return []
        if not choices:
            return []

        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        out: list[str] = []
        reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning_delta, str) and reasoning_delta:
            out.extend(self._reasoning_delta(reasoning_delta))
        content_delta = delta.get("content")
        if isinstance(content_delta, str) and content_delta:
            out.extend(self._text_delta(content_delta))
        for tc in (delta.get("tool_calls") or []):
            if isinstance(tc, dict):
                out.extend(self._tool_delta(tc))

        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]
            out.extend(self._close_reasoning())
            out.extend(self._close_text())
            out.extend(self._close_tools())
            if usage:
                self.usage = _usage_from_openai(usage)
            if self.usage is not None:
                # usage 同帧到达 → 完成事件立即发；否则等 usage 帧/[DONE]
                out.extend(self._send_completed(self._complete_status()))
        return out

    def _complete_status(self) -> str:
        return "incomplete" if self.finish_reason == "length" else "completed"

    def finish(self) -> list[str]:
        """流结束兜底：上游没发 finish/[DONE] 时补完成事件（不重复发）。"""
        out = self._close_reasoning() + self._close_text() + self._close_tools()
        out.extend(self._send_completed(self._complete_status()))
        return out


# ─────────────────────────── 流式：Anthropic typed 事件 ───────────────────────────


class AnthropicStreamAssembler:
    """OpenAI SSE 帧 → Anthropic typed 事件（9router response/openai-to-claude.js 移植）。

    事件序列：message_start →(content_block_start → delta* → content_block_stop)*
    → message_delta → message_stop。工具参数增量直接以 `input_json_delta` 转发；
    `message_delta` 等 usage 到达（或流结束）再发，避免把 input/output 冻结为 0。
    """

    def __init__(self, model: str, *, message_id: str | None = None):
        self.model = model
        self.message_id = message_id or f"msg_{uuid.uuid4().hex[:24]}"
        self.started = False
        self.stopped = False
        self.failed = False

        self.next_block_index = 0
        self.thinking_index: int | None = None
        self.thinking_open = False
        self.text_index: int | None = None
        self.text_open = False
        self.tool_blocks: dict[int, dict] = {}
        self.finish_reason: str | None = None
        self.usage: Usage | None = None

    def _emit(self, event_type: str, data: dict) -> str:
        data = {**data, "type": event_type}
        return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

    def _ensure_started(self) -> list[str]:
        if self.started:
            return []
        self.started = True
        return [self._emit("message_start", {
            "message": {"id": self.message_id, "type": "message", "role": "assistant",
                        "model": self.model, "content": [],
                        "stop_reason": None, "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0}},
        })]

    def _stop_thinking(self) -> list[str]:
        if not self.thinking_open:
            return []
        self.thinking_open = False
        return [self._emit("content_block_stop", {"index": self.thinking_index})]

    def _stop_text(self) -> list[str]:
        if not self.text_open:
            return []
        self.text_open = False
        return [self._emit("content_block_stop", {"index": self.text_index})]

    def _thinking_delta(self, delta: str) -> list[str]:
        if not delta:
            return []
        out = self._ensure_started()
        out.extend(self._stop_text())
        if not self.thinking_open:
            if self.thinking_index is None:
                self.thinking_index = self.next_block_index
                self.next_block_index += 1
            self.thinking_open = True
            out.append(self._emit("content_block_start", {
                "index": self.thinking_index,
                "content_block": {"type": "thinking", "thinking": ""},
            }))
        out.append(self._emit("content_block_delta", {
            "index": self.thinking_index,
            "delta": {"type": "thinking_delta", "thinking": delta},
        }))
        return out

    def _text_delta(self, delta: str) -> list[str]:
        if not delta:
            return []
        out = self._ensure_started()
        out.extend(self._stop_thinking())
        if not self.text_open:
            if self.text_index is None:
                self.text_index = self.next_block_index
                self.next_block_index += 1
            self.text_open = True
            out.append(self._emit("content_block_start", {
                "index": self.text_index,
                "content_block": {"type": "text", "text": ""},
            }))
        out.append(self._emit("content_block_delta", {
            "index": self.text_index,
            "delta": {"type": "text_delta", "text": delta},
        }))
        return out

    def _tool_delta(self, tc: dict) -> list[str]:
        try:
            idx = int(tc.get("index", 0))
        except (TypeError, ValueError):
            idx = 0
        fn = tc.get("function") or {}
        out = self._ensure_started()
        out.extend(self._stop_thinking())
        out.extend(self._stop_text())
        slot = self.tool_blocks.setdefault(
            idx, {"block_index": None, "id": "", "name": "", "buf": "",
                  "open": False, "stopped": False})
        if tc.get("id"):
            slot["id"] = str(tc["id"])
        if fn.get("name"):
            slot["name"] = str(fn["name"])
        if not slot["open"] and slot["id"] and slot["name"]:
            slot["block_index"] = self.next_block_index
            self.next_block_index += 1
            slot["open"] = True
            out.append(self._emit("content_block_start", {
                "index": slot["block_index"],
                "content_block": {"type": "tool_use", "id": slot["id"],
                                  "name": slot["name"], "input": {}},
            }))
            if slot["buf"]:
                out.append(self._emit("content_block_delta", {
                    "index": slot["block_index"],
                    "delta": {"type": "input_json_delta", "partial_json": slot["buf"]},
                }))
        args_delta = fn.get("arguments")
        if args_delta is not None and args_delta != "":
            args_delta = str(args_delta)
            slot["buf"] += args_delta
            if slot["open"]:
                out.append(self._emit("content_block_delta", {
                    "index": slot["block_index"],
                    "delta": {"type": "input_json_delta", "partial_json": args_delta},
                }))
        return out

    def _stop_tools(self) -> list[str]:
        out: list[str] = []
        for idx in sorted(self.tool_blocks.keys()):
            slot = self.tool_blocks[idx]
            if slot["open"] and not slot["stopped"]:
                slot["stopped"] = True
                out.append(self._emit("content_block_stop", {"index": slot["block_index"]}))
        return out

    def _message_delta(self) -> list[str]:
        if self.stopped or self.failed:
            return []
        self.stopped = True
        usage = self.usage
        return [
            self._emit("message_delta", {
                "delta": {"stop_reason": _ANTHROPIC_FINISH_FROM_CHAT.get(
                    self.finish_reason or "stop", "end_turn"), "stop_sequence": None},
                "usage": {"input_tokens": usage.prompt_tokens if usage else 0,
                          "output_tokens": usage.completion_tokens if usage else 0},
            }),
            self._emit("message_stop", {}),
        ]

    def failed_event(self, message: str, status: Any = None) -> list[str]:
        """上游中途失败 → `error` typed 事件（SSE 头已发时的收尾）。"""
        if self.failed or self.stopped:
            return []
        self.failed = True
        out = self._ensure_started()
        out.append(self._emit("error", {
            "error": {"type": "api_error", "message": str(message)[:1000]},
        }))
        return out

    def feed(self, sse_frame: str) -> list[str]:
        payload = _parse_sse_data(sse_frame)
        if payload is None:
            return []
        if payload == "[DONE]":
            out = self._stop_thinking() + self._stop_text() + self._stop_tools()
            out.extend(self._message_delta())
            return out
        if not isinstance(payload, dict):
            return []
        if payload.get("error"):
            err = payload["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            code = err.get("code") if isinstance(err, dict) else None
            return self.failed_event(str(msg or "upstream error"), code)

        choices = payload.get("choices") or []
        usage = payload.get("usage")
        if usage and not choices:
            self.usage = _usage_from_openai(usage)
            if self.finish_reason:
                return self._message_delta()
            return []
        if not choices:
            return []

        choice = choices[0] or {}
        delta = choice.get("delta") or {}
        out: list[str] = []
        reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
        if isinstance(reasoning_delta, str) and reasoning_delta:
            out.extend(self._thinking_delta(reasoning_delta))
        content_delta = delta.get("content")
        if isinstance(content_delta, str) and content_delta:
            out.extend(self._text_delta(content_delta))
        for tc in (delta.get("tool_calls") or []):
            if isinstance(tc, dict):
                out.extend(self._tool_delta(tc))

        if choice.get("finish_reason"):
            self.finish_reason = choice["finish_reason"]
            if usage:
                self.usage = _usage_from_openai(usage)
            out.extend(self._stop_thinking())
            out.extend(self._stop_text())
            out.extend(self._stop_tools())
            if self.usage is not None:
                # usage 同帧到达 → 立即收尾；否则等 usage 帧/[DONE]（不冻结 0）
                out.extend(self._message_delta())
        return out

    def finish(self) -> list[str]:
        out = self._stop_thinking() + self._stop_text() + self._stop_tools()
        out.extend(self._message_delta())
        return out
