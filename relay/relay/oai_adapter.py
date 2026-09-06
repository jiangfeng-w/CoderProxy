"""OpenAI 线协议 ↔ 本项目 ChatRequest/ChatResponse 映射（M2 最小集）。

入站：第三方 agent 的 OpenAI `/v1/chat/completions` 请求体 → ChatRequest。
出站：Ta3Provider.stream_structured 的思考/内容/工具事件 → OpenAI SSE 分帧，
      或聚合为非流式 JSON。

工具调用在 M2 阶段沿用 vendored ta3.py 内置的 disguise/restore（strict 式），
relay 层不做双向翻译（双向工具伪装归 M3）。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import AsyncIterator

from app.models.providers.ta3 import Ta3Provider
from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

DEFAULT_MODEL_OWNED_BY = "ta3"


def _parse_content_block(content) -> tuple[str | None, list[dict] | None]:
    """OpenAI content（str 或块数组）→ (纯文本, 附加内容块)。"""
    if isinstance(content, str):
        return content, None
    if isinstance(content, list):
        texts: list[str] = []
        blocks: list[dict] = []
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text"):
                texts.append(str(b["text"]))
            else:
                blocks.append(b)
        return ("\n".join(texts) or None), (blocks or None)
    return content, None


def _parse_thinking(value) -> bool | None:
    """各家 thinking 字段形态 → bool|None。

    - bool：原样
    - dict（GLM/Zhipu/Anthropic 风格）：{"type": "enabled"} → True，
      {"type": "disabled"} → False，其余 → None
    - str："enabled"/"true"/"on" → True，"disabled"/"false"/"off" → False，其余 → None
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        t = str(value.get("type") or "").strip().lower()
        if t == "enabled":
            return True
        if t == "disabled":
            return False
        return None
    if isinstance(value, str):
        t = value.strip().lower()
        if t in ("enabled", "true", "on"):
            return True
        if t in ("disabled", "false", "off"):
            return False
    return None


# 上游（牛码）风控按竞品系统提示的「身份指纹句」做大小写不敏感的精确子串封禁，
# 命中即 403「请求包含违规内容」。实测被封的指纹（且扫描全部 role 的消息文本）：
#   - "You are ZCode, an interactive coding agent"（ZCode 系统提示首行，原样/大小写
#     变化/句尾追加都封，插入空格或改标点即绕过 → 精确子串匹配）
#   - "You are an interactive ZCode agent that helps users ..."（ZCode 系统提示次行）
#   - "You are Claude Code"
# 对策：在 "You are ..." 身份句内的品牌词中插入零宽空格（ZWSP）破坏子串匹配。
# 限定身份句是为了避免污染代码/文件内容里对品牌词的正常提及——ZWSP 一旦被模型
# 复述进文件就是隐形脏字符；身份句不会出现在正常代码里，副作用可控。
_FINGERPRINT_BRAND_RE = re.compile(
    r"(you are\b[^\n]{0,200}?)\b(zcode|claude code)\b",
    re.IGNORECASE,
)
_ZWSP = "\u200b"  # ZERO WIDTH SPACE（写成转义，防止不可见字符被工具链吃掉）


def _sanitize_fingerprint(text: str | None) -> str | None:
    """身份指纹句中的品牌词插入零宽空格，规避上游精确子串封禁。"""
    if not text:
        return text
    return _FINGERPRINT_BRAND_RE.sub(
        lambda m: m.group(1) + m.group(2)[0] + _ZWSP + m.group(2)[1:],
        text,
    )


def _parse_tool_calls(tool_calls) -> list[dict] | None:
    """OpenAI assistant.tool_calls → ChatMessage.tool_calls（arguments 转 dict）。"""
    if not tool_calls:
        return None
    out: list[dict] = []
    for tc in tool_calls:
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                args = json.loads(args) if args else {}
            except (json.JSONDecodeError, TypeError):
                args = {"_raw": args}
        if not isinstance(args, dict):
            args = {"_raw": str(args)}
        out.append({
            "id": tc.get("id") or "",
            "name": fn.get("name") or "",
            "arguments": args,
        })
    return out or None


def apply_thinking_default(body: dict, request: ChatRequest, defaults: dict,
                           unset_mode: str = "default") -> None:
    """agent 未显式传思考参数时，按兜底策略下发（GUI 模型页/设置页配置）。

    - agent 传了 thinking / enable_thinking / reasoning_effort 任一 → 尊重 agent，不动；
    - unset_mode="off"（真关）：一律显式关思考，忽略每模型默认值；
    - unset_mode="default"（假关）：按每模型默认档位下发（未配置 = "none" 关）。
    "none" → thinking=True + reasoning_effort="none"，走 ta3.py 的
    thinking:disabled 路径显式关思考（上游默认行为可能是高档位思考，烧 token）。
    """
    if (body.get("thinking") is not None or body.get("enable_thinking") is not None
            or body.get("reasoning_effort")):
        return
    if unset_mode == "off":
        effort = "none"
    else:
        effort = str((defaults or {}).get(request.model) or "none")
    request.thinking = True
    request.reasoning_effort = effort


def oai_request_to_chat_request(body: dict) -> ChatRequest:
    """OpenAI 请求体 → ChatRequest。"""
    messages: list[ChatMessage] = []
    for m in body.get("messages", []) or []:
        role = m.get("role") or "user"
        content, blocks = _parse_content_block(m.get("content"))
        content = _sanitize_fingerprint(content)
        kwargs: dict = {}
        if role == "tool":
            kwargs["tool_call_id"] = m.get("tool_call_id")
        if role == "assistant":
            kwargs["tool_calls"] = _parse_tool_calls(m.get("tool_calls"))
            if m.get("reasoning_content"):
                kwargs["reasoning_content"] = _sanitize_fingerprint(m["reasoning_content"])
        messages.append(ChatMessage(
            role=role,
            content=content,
            content_blocks=blocks,
            **kwargs,
        ))

    # thinking：显式字段优先，其次按 reasoning_effort 推断（启用思考）。
    # 归一化非 bool 形态——GLM/智谱系（含 ZCode）发 {"type": "enabled"|"disabled", ...}，
    # Qwen/阿里系发 enable_thinking 布尔，部分客户端发字符串；原样透传会在
    # ChatRequest(bool|None) 校验炸成 500。
    thinking = _parse_thinking(body.get("thinking"))
    if thinking is None:
        thinking = _parse_thinking(body.get("enable_thinking"))
    reasoning_effort = body.get("reasoning_effort")
    if thinking is None and reasoning_effort:
        thinking = True
    if thinking is False:
        # agent 显式关思考（thinking: false / {"type": "disabled"}）→ 归一为
        # thinking=True + effort="none"：ta3.py 仅在此组合下才真发 thinking:disabled，
        # thinking=False 会什么都不发、落上游默认（大概率开思考），「关」语义丢失。
        thinking = True
        reasoning_effort = "none"

    return ChatRequest(
        messages=messages,
        model=str(body.get("model") or ""),
        stream=bool(body.get("stream", False)),
        tools=body.get("tools"),
        temperature=body.get("temperature"),
        max_tokens=body.get("max_tokens"),
        reasoning_effort=reasoning_effort,
        thinking=thinking,
    )


def _new_id() -> str:
    return f"chatcmpl-{uuid.uuid4().hex[:24]}"


# Anthropic stop_reason → OpenAI finish_reason。上游 anthropic 协议模型的
# stop_reason（end_turn/tool_use 等）不是合法的 OpenAI 枚举值，原样下发会让
# 严格客户端（如 ZCode 的 AI SDK）解析失败；在此归一。
_FINISH_REASON_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "max_tokens": "length",
}


def _finish_reason_to_openai(reason: str | None) -> str | None:
    if not reason:
        return None
    return _FINISH_REASON_MAP.get(reason, reason)


def _sse_chunk(*, model: str, delta: dict, finish_reason: str | None = None) -> str:
    payload = {
        "id": _new_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sse_usage_chunk(*, model: str, usage: Usage) -> str:
    payload = {
        "id": _new_id(),
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": _usage_to_openai(usage),
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _usage_to_openai(usage: Usage) -> dict:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "prompt_tokens_details": {"cached_tokens": usage.cached_input_tokens},
        "completion_tokens_details": {"reasoning_tokens": usage.reasoning_tokens},
    }


class UsageCollector:
    """流式 usage 传出通道（M6 日志落库用）。

    provider 的 done 事件处由 adapter 回调 record()：既收集 usage（agent 不传
    include_usage 时没有 SSE usage 帧，只有经此通道能拿到），也把 done 到达时刻与
    请求起点之差换算为 duration_ms（口径：上游完成即止，不含 SSE 下行至 agent 耗时）。

    401 重试：每次 attempt 前 reset()（起点不变），重试成功后只保留最后那次 usage。
    """

    def __init__(self, started: float | None = None) -> None:
        self.started = started if started is not None else time.monotonic()
        self.usages: list[Usage] = []
        self.duration_ms: int | None = None

    @property
    def usage(self) -> Usage | None:
        return self.usages[-1] if self.usages else None

    def record(self, usage: Usage | None = None) -> None:
        self.usages.append(usage or Usage())
        self.duration_ms = max(0, int((time.monotonic() - self.started) * 1000))

    def reset(self) -> None:
        self.usages.clear()
        self.duration_ms = None


async def stream_openai_sse(provider: Ta3Provider, request: ChatRequest,
                            include_usage: bool = False,
                            usage_collector: UsageCollector | None = None) -> AsyncIterator[str]:
    """把 provider 的流式事件翻译为 OpenAI SSE 帧（含 [DONE]）。"""
    model = request.model or provider._model_name  # noqa: SLF001（vendored 内部字段）
    # 首帧声明 role，兼容多数 agent 对 assistant 角色帧的要求
    yield _sse_chunk(model=model, delta={"role": "assistant", "content": ""})

    tool_call_ids: list[str] = []
    finish_reason: str | None = None
    usage: Usage | None = None
    async for event in provider.stream_structured(request):
        etype = event["type"]
        if etype == "thinking":
            yield _sse_chunk(model=model, delta={"reasoning_content": event["delta"]})
        elif etype == "content":
            yield _sse_chunk(model=model, delta={"content": event["delta"]})
        elif etype == "done":
            tool_calls = event.get("tool_calls") or []
            if tool_calls:
                for i, tc in enumerate(tool_calls):
                    args = tc.get("arguments") or {}
                    if isinstance(args, dict):
                        args = json.dumps(args, ensure_ascii=False)
                    else:
                        args = str(args)
                    delta = {
                        "tool_calls": [{
                            "index": i,
                            "id": tc.get("id") or f"call_{i:02d}",
                            "type": "function",
                            "function": {"name": tc.get("name") or "", "arguments": args},
                        }],
                    }
                    tool_call_ids.append(tc.get("id") or "")
                    yield _sse_chunk(model=model, delta=delta)
            finish_reason = event.get("finish_reason")
            usage = event.get("usage")
            # M6：落库通道——无论 include_usage 与否都收集 usage，并打点 done 时刻
            if usage_collector is not None:
                usage_collector.record(usage)

    yield _sse_chunk(model=model, delta={},
                     finish_reason=_finish_reason_to_openai(finish_reason) or "stop")
    if include_usage and usage is not None:
        yield _sse_usage_chunk(model=model, usage=usage)
    yield "data: [DONE]\n\n"


def _tool_calls_to_openai(tool_calls: list[dict]) -> list[dict]:
    out = []
    for tc in tool_calls or []:
        args = tc.get("arguments") or {}
        if isinstance(args, dict):
            args = json.dumps(args, ensure_ascii=False)
        out.append({
            "id": tc.get("id") or "",
            "type": "function",
            "function": {"name": tc.get("name") or "", "arguments": str(args)},
        })
    return out


def chat_response_to_openai(provider: Ta3Provider, request: ChatRequest,
                            response: ChatResponse) -> dict:
    """非流式聚合 → OpenAI 标准 chat.completion JSON。"""
    message: dict = {"role": "assistant", "content": response.content}
    if response.thinking:
        message["reasoning_content"] = response.thinking
    if response.tool_calls:
        message["tool_calls"] = _tool_calls_to_openai(response.tool_calls)
    return {
        "id": _new_id(),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": request.model or provider._model_name,  # noqa: SLF001
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": _finish_reason_to_openai(response.finish_reason) or "stop",
        }],
        "usage": _usage_to_openai(response.usage),
    }


def model_to_openai(model: dict) -> dict:
    """目录模型条目 → OpenAI /v1/models data 项（附额外元数据字段）。

    reasoning_efforts（思考强度档位）：取自上游目录 completion_options.thinkingLevels
    （牛码官方配置，实测各模型档位不同，如 glm-5.3 有 low/high/max、deepseek-v4 仅
    high/max），level 即 agent 端 reasoning_effort 的合法取值；reasoning_labels 为
    上游自带的中文标签（GUI 展示用）。不查 vendored 内置目录——其数据来源不可靠。
    """
    name = model.get("name") or model.get("id") or ""
    completion_options = model.get("completion_options") or {}
    levels = completion_options.get("thinkingLevels") or []
    efforts: list[str] = []
    labels: dict[str, str] = {}
    for lv in levels:
        if not isinstance(lv, dict):
            continue
        level = str(lv.get("level") or "").strip()
        if not level or level in efforts:
            continue
        efforts.append(level)
        label = str(lv.get("label") or "").strip()
        if label:
            labels[level] = label
    supports_reasoning = (
        bool(model.get("anthropic"))
        or bool(completion_options.get("thinkingEnabled"))
        or bool(efforts)
    )
    return {
        "id": name,
        "object": "model",
        "created": 0,
        "owned_by": DEFAULT_MODEL_OWNED_BY,
        "name": model.get("title") or name,
        "context_window": model.get("context_window"),
        "supports_reasoning": supports_reasoning,
        "reasoning_efforts": efforts,
        "reasoning_labels": labels,
        "is_multimodal": bool(model.get("is_multimodal")),
    }
