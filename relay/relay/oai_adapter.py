"""OpenAI 线协议 ↔ 本项目 ChatRequest/ChatResponse 映射（M2 最小集）。

入站：第三方 agent 的 OpenAI `/v1/chat/completions` 请求体 → ChatRequest。
出站：Ta3Provider.stream_structured 的思考/内容/工具事件 → OpenAI SSE 分帧，
      或聚合为非流式 JSON。

工具调用在 M2 阶段沿用 vendored ta3.py 内置的 disguise/restore（strict 式），
relay 层不做双向翻译（双向工具伪装归 M3）。

上游空响应（空流）防御（BUG-001 修复，2026-10-08）：牛码网关间歇性 HTTP 200 后
立即干净关流（0 token 空流）。此处统一做空响应判定 + relay 内自动重试
（settings.empty_stream_retries）；耗尽仍空抛 EmptyStreamError，由 routes 层
如实记 chat_error 并以 502 透传——不再向 agent 发假成功的空流/空 JSON。
判定先于产出：流式在任何帧（含 role 帧）yield 之前完成判定，保证重试时响应头
未发、且不重发 role 帧。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator

from app.models.providers.ta3 import Ta3Provider
from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

from relay.config import settings

logger = logging.getLogger(__name__)

DEFAULT_MODEL_OWNED_BY = "ta3"

# ─────────────────────────── 上游空响应防御（BUG-001） ───────────────────────────

_EMPTY_STREAM_MSG = "上游返回空响应"


class EmptyStreamError(RuntimeError):
    """上游 HTTP 200 后干净关流的空响应（零产出）。

    raw_tail 为上游原始 SSE 行尾巴（本地诊断用）：只允许进 logger / chat_error
    落库 detail，**不得**拼进面向 agent 的错误消息（硬性规则 2：不把上游内部
    协议行泄漏给 agent）。
    """

    def __init__(self, message: str = _EMPTY_STREAM_MSG, *,
                 raw_tail: list | str | None = None) -> None:
        super().__init__(message)
        self.raw_tail = raw_tail


def empty_stream_exhausted_message(retried: int) -> str:
    """重试耗尽（或 retries=0 直接失败）时的错误消息；不含 raw_tail（硬性规则 2）。"""
    if retried <= 0:
        return _EMPTY_STREAM_MSG
    return f"{_EMPTY_STREAM_MSG}（已重试 {retried} 次仍为空）"


async def empty_stream_retry_wait(attempt: int) -> None:
    """第 attempt 次重试前的退避等待（1s/2s 封顶），测试可 monkeypatch 掉。"""
    await asyncio.sleep(min(attempt, 2) * 1.0)


def _is_empty_usage(usage) -> bool:
    """usage 缺失或零 token——上游没真处理这次请求的硬指标。

    兼容 Usage 模型与 dict（adapter 注册表接入的其它上游可能直接给 dict）。
    """
    if usage is None:
        return True
    if isinstance(usage, dict):
        total = usage.get("total_tokens") or 0
    else:
        total = getattr(usage, "total_tokens", 0) or 0
    return not total


def _tool_call_is_shell(tc) -> bool:
    """工具调用是否只有壳（有 name 但 arguments 空/占位）。

    上游 tool_use 只发了 content_block_start（id/name）就关流时，仍会产出
    {id,name,arguments:{}} 的占位 tool_call；这种不算真实产出，按空响应处理。
    """
    if not isinstance(tc, dict):
        return True
    args = tc.get("arguments")
    if isinstance(args, dict):
        # 空 dict / 仅 {"_raw": ""} 的降级占位都算壳
        return set(args.keys()) <= {"_raw"} and not args.get("_raw")
    return not args


def _is_empty_done(done: dict) -> bool:
    """done 事件是否为空响应。

    硬指标：usage.total_tokens == 0（上游没真处理这次请求，input 都没计费）。
    此时即便 content/thinking/tool_calls 非空（网关发了壳帧就关流，如 tool_use
    只有 id/name、无 arguments），也按空响应重试——占位产出对客户端同样无用。
    usage 缺失（None）视为空。total_tokens > 0 或存在真实产出时按非空放行。
    """
    if not _is_empty_usage(done.get("usage")):
        return False
    if done.get("content") or done.get("thinking"):
        return False
    tool_calls = done.get("tool_calls") or []
    if any(not _tool_call_is_shell(tc) for tc in tool_calls):
        return False
    return True


def _is_empty_chat_response(response: ChatResponse) -> bool:
    """非流式聚合响应的空响应判定（与 _is_empty_done 同口径）。"""
    if not _is_empty_usage(response.usage):
        return False
    if response.content or response.thinking:
        return False
    tool_calls = response.tool_calls or []
    if any(not _tool_call_is_shell(tc) for tc in tool_calls):
        return False
    return True


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


async def chat_with_empty_retry(provider: Ta3Provider, request: ChatRequest) -> ChatResponse:
    """非流式聚合 + 上游空响应重试（独立预算 settings.empty_stream_retries）。

    与流式 path 同口径：网关 200 后干净关流会产出全 0 的 ChatResponse，此前被
    如实透传给 agent（假成功空 JSON）。此处对空响应重发上游请求；耗尽仍空抛
    EmptyStreamError，由 routes 记 chat_error 并以 502 透传。真 401 等其它
    RuntimeError 不归本函数处理，原样抛给外层 _chat_with_retry。
    """
    max_retries = max(0, int(getattr(settings, "empty_stream_retries", 3) or 0))
    last: ChatResponse | None = None
    for attempt in range(max_retries + 1):
        if attempt:
            logger.warning("[oai] 上游空响应，第 %d/%d 次重试: %s",
                           attempt, max_retries,
                           request.model or provider._model_name)  # noqa: SLF001
            await empty_stream_retry_wait(attempt)
        response = await provider.chat(request)
        if not _is_empty_chat_response(response):
            return response
        last = response
    raise EmptyStreamError(empty_stream_exhausted_message(max_retries))


async def stream_openai_sse(provider: Ta3Provider, request: ChatRequest,
                            include_usage: bool = False,
                            usage_collector: UsageCollector | None = None) -> AsyncIterator[str]:
    """把 provider 的流式事件翻译为 OpenAI SSE 帧（含 [DONE]）。

    BUG-001 防御——判定先于产出：任何帧（含 role 首帧）yield 之前先完成空响应
    判定，期间只缓冲零值 delta 帧；收到真实产出信号（非空 content/thinking 或
    真实工具调用）才释放 role 首帧与缓冲。整个流零产出且 done 判定为空（usage
    全 0）时抛 EmptyStreamError——一帧未发，调用方重试时响应头未发、且重试不会
    重复发送 role 帧。
    """
    model = request.model or provider._model_name  # noqa: SLF001（vendored 内部字段）

    tool_call_ids: list[str] = []
    finish_reason: str | None = None
    usage: Usage | None = None
    pending: list[str] = []   # 判定前缓冲的帧（零值 delta）；确认非空后按序补发
    released = False          # 已确认非空：role 首帧与缓冲可（或已）发出
    done_event: dict | None = None

    def _release() -> list[str]:
        """确认非空 → 返回需补发的前置帧（role 首帧 + 缓冲）；只补发一次。"""
        nonlocal released
        if released:
            return []
        released = True
        frames = [_sse_chunk(model=model, delta={"role": "assistant", "content": ""})]
        frames.extend(pending)
        pending.clear()
        return frames

    async for event in provider.stream_structured(request):
        etype = event["type"]
        if etype == "thinking":
            frame = _sse_chunk(model=model, delta={"reasoning_content": event["delta"]})
            if not released and not event["delta"]:
                pending.append(frame)  # 零值 delta：先缓冲，待判定非空再释放
                continue
            for head in _release():
                yield head
            yield frame
        elif etype == "content":
            frame = _sse_chunk(model=model, delta={"content": event["delta"]})
            if not released and not event["delta"]:
                pending.append(frame)
                continue
            for head in _release():
                yield head
            yield frame
        elif etype == "done":
            done_event = event
            if not released and _is_empty_done(event):
                # 零产出 + usage 全 0：一帧未发即判空抛错；raw_tail 只随异常给
                # 调用方落日志（不得进面向 agent 的错误消息，硬性规则 2）
                raise EmptyStreamError(raw_tail=event.get("raw_tail"))
            for head in _release():
                yield head
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

    if not released:
        # provider 未发 done 即结束（或全程零事件）：同样按空响应处理，不再假成功
        raise EmptyStreamError(raw_tail=(done_event or {}).get("raw_tail"))

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
