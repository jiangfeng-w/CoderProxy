"""自定义供应商转发生效（「WorkBuddy聊天反代与多平台聚合」§4.4 落地）。

供应商页 F5 配置的条目（`platform_custom.json`：{id,name,base_url,api_key,enabled,
models[]}）在此接成出站 adapter：**零代码纯配置**（9router openai-compatible-* 节点
即此做法）——只做传输（URL/头/auth/retry），协议翻译在出口管道（`oai_adapter`）。

- 端点约定：`POST {base_url}/chat/completions`（兼容 base_url 已含 `/v1` 写法：
  尾斜杠归一后拼接，避免 `//`）；
- 鉴权：`Authorization: Bearer <api_key>`（与既有 `require_api_key` 语义一致的
  OpenAI 兼容约定）；
- 流式：`stream:true` 原样转发，上游 SSE 帧按白名单重建（只留
  `id/object/created/model/choices[].delta|finish_reason/usage`，剥供应商扩展字段）；
- 非流式：上游 JSON 原样透传（不做重写，供应商即 OpenAI 兼容）；
- 错误：HTTP 非 2xx → UpstreamHttpError（routes 透传真实状态码与上游 message）。
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator

import httpx

from app.models.schemas import ChatRequest, ChatResponse, Usage

from relay.config import settings

logger = logging.getLogger(__name__)

# 允许的 delta 字段（白名单重建；防供应商扩展字段泄漏给 agent，硬性规则 2）
_DELTA_KEYS = ("role", "content", "reasoning_content", "tool_calls")


class UpstreamHttpError(RuntimeError):
    """上游 HTTP 错误（routes 依据 `.status` 透传状态码）。"""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


class OpenAICompatProvider:
    """自定义供应商出站 adapter（默认实现只管传输，无平台特化钩子）。

    entry：供应商配置条目（含 api_key，仅 relay 内部用）。
    """

    def __init__(self, entry: dict, bare_model: str, *,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._entry = entry or {}
        self._model = str(bare_model or "")
        self._transport = transport

    # ─────────────────────── 传输 ───────────────────────

    def _url(self) -> str:
        base = str(self._entry.get("base_url") or "").rstrip("/")
        if base.endswith("/chat/completions"):
            # 用户把完整端点连进 base_url：不再二次拼接（宽容）
            return base
        return f"{base}/chat/completions"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json",
                   "Accept": "application/json, text/event-stream"}
        key = str(self._entry.get("api_key") or "")
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(trust_env=False,
                                 timeout=httpx.Timeout(300.0, connect=15.0),
                                 transport=self._transport)

    def _body(self, request: ChatRequest, *, stream: bool) -> dict:
        body: dict = {
            "model": self._model,
            "messages": [_message_payload(m) for m in request.messages],
            "stream": stream,
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens:
            body["max_tokens"] = request.max_tokens
        if request.tools:
            body["tools"] = request.tools
        if request.reasoning_effort:
            body["reasoning_effort"] = request.reasoning_effort
        return body

    # ─────────────────────── 出站事件契约 ───────────────────────

    async def stream_structured(self, request: ChatRequest) -> AsyncIterator[dict]:
        """上游 SSE（OpenAI 兼容）→ relay 事件。"""
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        finish_reason: str | None = None
        usage: Usage | None = None
        raw_tail: list[str] = []

        async with self._new_client() as client:
            async with client.stream("POST", self._url(), headers=self._headers(),
                                     json=self._body(request, stream=True)) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    raise UpstreamHttpError(_error_text(resp.status_code, text),
                                            status=resp.status_code)
                async for line in resp.aiter_lines():
                    frame = _parse_sse_line(line)
                    if frame is None:
                        continue
                    raw_tail = [*raw_tail[-7:],
                                json.dumps(frame, ensure_ascii=False, default=str)[:300]]
                    if frame.get("usage"):
                        usage = _usage_from_upstream(frame["usage"])
                    if frame.get("finish_reason"):
                        finish_reason = str(frame["finish_reason"])
                    if frame.get("content"):
                        content_parts.append(frame["content"])
                        yield {"type": "content", "delta": frame["content"]}
                    if frame.get("reasoning_content"):
                        thinking_parts.append(frame["reasoning_content"])
                        yield {"type": "thinking", "delta": frame["reasoning_content"]}
                    for tc in frame.get("tool_calls") or []:
                        _merge_tool_call(tool_calls, tc)
                        yield {"type": "tool_call", "delta": tc}

        yield {
            "type": "done",
            "content": "".join(content_parts) or None,
            "thinking": "".join(thinking_parts) or None,
            "tool_calls": _assemble_tool_calls(tool_calls),
            "finish_reason": finish_reason or "stop",
            "usage": usage or Usage(),
            "raw_tail": raw_tail,
        }

    async def chat(self, request: ChatRequest) -> ChatResponse:
        """非流式：优先原样转发上游 JSON；上游不支持非流式（4xx）再退回流式聚合。"""
        body = self._body(request, stream=False)
        async with self._new_client() as client:
            resp = await client.post(self._url(), headers=self._headers(), json=body)
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError as exc:
                    raise UpstreamHttpError("上游响应非 JSON", status=502) from exc
                return _chat_response_from_json(data, self._model)
            text = resp.text
            # 4xx（如「不支持非流式」）：退回流式聚合，宽容供应商差异
            if 400 <= resp.status_code < 500:
                logger.info("[custom] 非流式被拒（%d），退回流式聚合", resp.status_code)
                return await _aggregate_stream(self, request)
            raise UpstreamHttpError(_error_text(resp.status_code, text),
                                    status=resp.status_code)


async def _aggregate_stream(adapter: OpenAICompatProvider,
                            request: ChatRequest) -> ChatResponse:
    response = ChatResponse(content=None, tool_calls=[], finish_reason="stop",
                            usage=Usage())
    async for event in adapter.stream_structured(request):
        if event["type"] == "done":
            response = ChatResponse(
                content=event["content"], thinking=event["thinking"],
                tool_calls=event["tool_calls"], finish_reason=event["finish_reason"],
                usage=event["usage"], model=adapter._model)
    return response


# ─────────────────────── 模块级工具 ───────────────────────

def _message_payload(m) -> dict:
    """ChatMessage → OpenAI 线格式（developer 归一到 system）。"""
    payload: dict = {"role": "system" if m.role == "developer" else m.role}
    if m.content_blocks:
        blocks: list[dict] = []
        if m.content:
            blocks.append({"type": "text", "text": m.content})
        blocks.extend(m.content_blocks)
        payload["content"] = blocks
    else:
        payload["content"] = m.content
    if m.role == "tool" and m.tool_call_id:
        payload["tool_call_id"] = m.tool_call_id
    if m.role == "assistant" and m.tool_calls:
        payload["tool_calls"] = [
            {"id": tc.get("id") or "", "type": "function",
             "function": {"name": tc.get("name") or "",
                          "arguments": _arguments_str(tc.get("arguments"))}}
            for tc in m.tool_calls]
    if m.reasoning_content:
        payload["reasoning_content"] = m.reasoning_content
    return payload


def _arguments_str(args) -> str:
    if isinstance(args, str):
        return args
    if args is None:
        return "{}"
    return json.dumps(args, ensure_ascii=False)


def _parse_sse_line(line: str) -> dict | None:
    """OpenAI 兼容 SSE 行 → 精简帧（白名单重建）。"""
    text = str(line or "").strip()
    if not text or text.startswith(":") or not text.startswith("data:"):
        return None
    payload = text[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        obj = json.loads(payload)
    except ValueError:
        return None
    if not isinstance(obj, dict):
        return None
    out: dict = {}
    for ch in obj.get("choices") or []:
        if not isinstance(ch, dict):
            continue
        delta = ch.get("delta") or {}
        for key in _DELTA_KEYS:
            if isinstance(delta.get(key), str) and delta[key]:
                out[key] = delta[key]
        if isinstance(delta.get("tool_calls"), list):
            out["tool_calls"] = [_slim_tool_call(tc) for tc in delta["tool_calls"]
                                 if isinstance(tc, dict)]
        if ch.get("finish_reason"):
            out["finish_reason"] = str(ch["finish_reason"])
        if isinstance(ch.get("message"), dict):  # 个别供应商流式里混 message 形态
            msg = ch["message"]
            if isinstance(msg.get("content"), str) and msg["content"]:
                out["content"] = msg["content"]
    if isinstance(obj.get("usage"), dict):
        out["usage"] = obj["usage"]
    return out or None


def _slim_tool_call(tc: dict) -> dict:
    fn = tc.get("function") or {}
    slim: dict = {"index": tc.get("index", 0), "type": tc.get("type") or "function"}
    if tc.get("id"):
        slim["id"] = str(tc["id"])
    if fn.get("name"):
        slim["name"] = str(fn["name"])
    if fn.get("arguments") is not None:
        slim["arguments"] = str(fn["arguments"])
    return slim


def _usage_from_upstream(usage: dict) -> Usage:
    details = usage.get("completion_tokens_details") or {}
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        reasoning_tokens=int(details.get("reasoning_tokens") or 0),
    )


def _merge_tool_call(acc: dict[int, dict], tc: dict) -> None:
    idx = int(tc.get("index") or 0)
    cur = acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
    if tc.get("id"):
        cur["id"] = str(tc["id"])
    if tc.get("name"):
        cur["name"] = str(tc["name"])
    if tc.get("arguments"):
        cur["arguments"] += str(tc["arguments"])


def _assemble_tool_calls(acc: dict[int, dict]) -> list[dict]:
    out: list[dict] = []
    for idx in sorted(acc):
        cur = acc[idx]
        try:
            args: object = json.loads(cur["arguments"]) if cur["arguments"] else {}
        except ValueError:
            args = cur["arguments"]
        out.append({"id": cur["id"] or f"call_{idx:02d}", "name": cur["name"],
                    "arguments": args})
    return out


def _chat_response_from_json(data: dict, model: str) -> ChatResponse:
    """上游非流式 OpenAI JSON → ChatResponse（宽容缺字段）。"""
    choices = data.get("choices") or [{}]
    ch = choices[0] if isinstance(choices[0], dict) else {}
    msg = ch.get("message") or {}
    tool_calls: list[dict] = []
    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments")
        try:
            args: object = json.loads(raw_args) if isinstance(raw_args, str) else (
                raw_args or {})
        except ValueError:
            args = raw_args
        tool_calls.append({"id": tc.get("id") or "", "name": fn.get("name") or "",
                           "arguments": args})
    usage = _usage_from_upstream(data.get("usage") or {})
    return ChatResponse(
        content=msg.get("content"),
        thinking=msg.get("reasoning_content"),
        tool_calls=tool_calls,
        finish_reason=str(ch.get("finish_reason") or "stop"),
        usage=usage,
        model=str(data.get("model") or model),
    )


def _error_text(status: int, body_text: str) -> str:
    """上游错误文本：尽量抠 error.message，抠不到退化为截断原文。"""
    try:
        data = json.loads(body_text)
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict) and err.get("message"):
                return str(err["message"])[:300]
            if data.get("message"):
                return str(data["message"])[:300]
    except ValueError:
        pass
    return (body_text or f"HTTP {status}").strip()[:300]


# ─────────────────────── 路由入口 ───────────────────────

def build_by_entry_id(entry_id: str, bare_model: str) -> OpenAICompatProvider:
    """同步构造（routes.build_adapter 用）：按条目 id 读 `platform_custom.json`。

    读文件是同步小 IO（与 providers_custom._load_sync 同法）；条目由
    `_ensure_provider_model` 已校验存在且 enabled，此处仅复读配置。
    """
    from relay import providers_custom
    entry = providers_custom.load_entry_sync(entry_id)
    if entry is None:
        raise KeyError(entry_id)
    return OpenAICompatProvider(entry, bare_model)


__all__ = ["OpenAICompatProvider", "UpstreamHttpError", "build_by_entry_id"]
