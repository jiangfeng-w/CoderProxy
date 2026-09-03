"""OpenAI 线协议 ↔ 本项目 ChatRequest/ChatResponse 映射（M2 最小集）。

入站：第三方 agent 的 OpenAI `/v1/chat/completions` 请求体 → ChatRequest。
出站：Ta3Provider.stream_structured 的思考/内容/工具事件 → OpenAI SSE 分帧，
      或聚合为非流式 JSON。

工具调用在 M2 阶段沿用 vendored ta3.py 内置的 disguise/restore（strict 式），
relay 层不做双向翻译（双向工具伪装归 M3）。
"""
from __future__ import annotations

import json
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


def oai_request_to_chat_request(body: dict) -> ChatRequest:
    """OpenAI 请求体 → ChatRequest。"""
    messages: list[ChatMessage] = []
    for m in body.get("messages", []) or []:
        role = m.get("role") or "user"
        content, blocks = _parse_content_block(m.get("content"))
        kwargs: dict = {}
        if role == "tool":
            kwargs["tool_call_id"] = m.get("tool_call_id")
        if role == "assistant":
            kwargs["tool_calls"] = _parse_tool_calls(m.get("tool_calls"))
            if m.get("reasoning_content"):
                kwargs["reasoning_content"] = m.get("reasoning_content")
        messages.append(ChatMessage(
            role=role,
            content=content,
            content_blocks=blocks,
            **kwargs,
        ))

    # thinking：显式字段优先，其次按 reasoning_effort 推断（启用思考）
    thinking = body.get("thinking")
    reasoning_effort = body.get("reasoning_effort")
    if thinking is None and reasoning_effort:
        thinking = True

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


async def stream_openai_sse(provider: Ta3Provider, request: ChatRequest,
                            include_usage: bool = False) -> AsyncIterator[str]:
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

    yield _sse_chunk(model=model, delta={}, finish_reason=finish_reason or "stop")
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
            "finish_reason": response.finish_reason or "stop",
        }],
        "usage": _usage_to_openai(response.usage),
    }


def model_to_openai(model: dict) -> dict:
    """目录模型条目 → OpenAI /v1/models data 项（附额外元数据字段）。"""
    name = model.get("name") or model.get("id") or ""
    return {
        "id": name,
        "object": "model",
        "created": 0,
        "owned_by": DEFAULT_MODEL_OWNED_BY,
        "name": model.get("title") or name,
        "context_window": model.get("context_window"),
        "supports_reasoning": bool(model.get("anthropic")) or bool(model.get("completion_options", {}).get("thinkingEnabled")),
        "is_multimodal": bool(model.get("is_multimodal")),
    }
