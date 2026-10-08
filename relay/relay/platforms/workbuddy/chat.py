"""WorkBuddy 聊天出站 adapter（「WorkBuddy聊天反代与多平台聚合」§3.2 / §4.2 落地）。

职责（对齐 9router `codebuddy-cn.js` 的「默认实现 + 薄钩子」模式）：

- **传输**：POST `{wb_api_base}/v2/chat/completions`（强 stream，B2 实测 400/11101）；
  桌面档头族（B1 实测 2026-10-08：UA 三段式 + 归属四头，与登录档位身份一致）；
- **请求改写钩子** `transform_request`：强制 `stream:true`；system 中性化（B3：agent
  身份句/超长 system 命中 11128 渠道风控 → 替换中性句，不重试）；`reasoning_effort`
  档位归一（B4：`none` 真关且不带 summary；`off`/`disabled` 删字段）；
- **错误识别钩子** `parse_error`：错误码族映射（B6：11101→400 / 11128→400 /
  11102→404 / 6004→429；未知→透传上游状态）；
- **响应翻译**：上游 SSE 帧白名单重建（剥离 `function_call`/`refusal`/`extra_fields`
  等腾讯内部字段——硬性规则 2）；usage 映射（B8）。

账号池选号（§4.3）：轮询 `status == "normal"` 账号；401/403 换号重试（每号最多一次，
全部试完抛最后错误）；无可用账号 → NoAvailableAccountError（routes 映射 503）。

非流式：上游仅支持流式（B2），本 adapter 内部收流聚合为 ChatResponse。
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator

import httpx

from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

from relay.config import settings
from relay.platforms import store
from relay.platforms.base import PlatformUpstreamError
from relay.platforms.workbuddy import oauth
from relay.platforms.workbuddy.adapter import WorkBuddyAdapter
from relay.platforms.workbuddy.client import CN_ORIGIN, derive_stable_id

logger = logging.getLogger(__name__)

PLATFORM_ID = "workbuddy"

CHAT_PATH = "/v2/chat/completions"

# ── B3：system 中性化（9router codebuddy-cn.js 同款口径） ──
NEUTRAL_SYSTEM_PROMPT = ("You are a helpful AI assistant that helps with "
                         "software engineering tasks.")
_SYSTEM_MAX_LEN = 2000
_AGENT_PATTERN = re.compile(
    r"you are claude code|claude.?code.+official.+cli|anthropic.+official.+cli"
    r"|you are (?:cursor|windsurf|cline|aider|continue|copilot|cody)"
    r"|you are an? (?:ai )?(?:coding |code )?agent"
    r"|cc_entrypoint\s*=\s*(?:cli|vscode|jetbrains|gui)"
    r"|claude.?code.+issues|give feedback.+claude.?code"
    r"|you are .{0,30}(?:powerful )?ai agent|orchestration capabilities"
    r"|OhMyOpenCode|<agent-identity>|<Role>|<Behavior_Instructions>",
    re.IGNORECASE)

# ── B6：错误码族（实测 2026-10-08）──
_CODE_11101_BAD_PARAMS = 11101  # 参数错 / 非流式
_CODE_11102_MODEL_MISSING = 11102  # 模型不可用
_CODE_11128_CHANNEL_BLOCKED = 11128  # 渠道风控（agent 身份句等）
_CODE_6004_RATE_LIMIT = 6004  # 限流（msg 常附恢复时间文案）

_platform_adapter = WorkBuddyAdapter()  # 复用账号层 ensure_token（per-uid 刷新锁）


class NoAvailableAccountError(RuntimeError):
    """账号池无可用账号（无 normal 账号 / 全部凭证被拒）。routes 映射 503。"""


class _CredentialRejected(RuntimeError):
    """本轮账号凭证被上游拒绝（401/403）：换号重试信号。"""


class WorkBuddyChatAdapter:
    """WorkBuddy 聊天出站 adapter（出站事件契约见 relay/adapter_registry.py）。

    bare_model：上游真实模型名（无前缀）；model_entry：目录条目（可空）。
    transport：测试注入点（httpx.MockTransport）。
    """

    def __init__(self, bare_model: str, *, ctx=None, model_entry: dict | None = None,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._model = str(bare_model or "")
        self._ctx = ctx          # 工具伪装上下文（B5：WorkBuddy 原生支持 tools，不做伪装）
        self._entry = model_entry or {}
        self._transport = transport
        self._active_uid = ""

    # ─────────────────────── 账号池选号（§4.3） ───────────────────────

    async def _normal_accounts(self) -> list[dict]:
        accounts = [a for a in await store.load_accounts(PLATFORM_ID)
                    if str(a.get("status") or "normal") == "normal"]
        # uid 排序 + 全局游标 → 跨请求轮转（同号不连续命中）
        return sorted(accounts, key=lambda a: str(a.get("uid") or ""))

    async def _pick_account(self, tried: set[str]) -> dict:
        accounts = [a for a in await self._normal_accounts()
                    if str(a.get("uid") or "") not in tried]
        if not accounts:
            raise NoAvailableAccountError("无可用 WorkBuddy 账号（请到供应商页添加/重登）")
        idx = _next_cursor() % len(accounts)
        return accounts[idx]

    async def refresh_credentials(self) -> bool:
        """401 刷新钩子（routes `_refresh_provider_credentials` 调用）：

        有 active 号 → 刷新该号；否则刷新全部 normal 号。返回是否至少成功一次。
        """
        if self._active_uid:
            accounts = [await store.find_account(PLATFORM_ID, self._active_uid)]
        else:
            accounts = await self._normal_accounts()
        ok = False
        for account in accounts:
            if not account:
                continue
            try:
                await oauth.refresh_token(account)
                ok = True
            except Exception as exc:  # noqa: BLE001（单号失败不阻断其它号）
                logger.warning("[wb-chat] 刷新账号 %s 失败: %s",
                               str(account.get("uid") or "")[:8], exc)
        return ok

    # ─────────────────────── 请求改写钩子 ───────────────────────

    def transform_request(self, request: ChatRequest) -> dict:
        """ChatRequest → 上游 chat/completions body。"""
        body: dict = {
            "model": self._model,
            "stream": True,  # B2：上游拒绝非流式（400/11101），relay 层做非流式聚合
            "messages": [_message_payload(m) for m in request.messages],
        }
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens:
            body["max_tokens"] = request.max_tokens
        if request.tools:
            body["tools"] = request.tools  # B5：原生透传（不做工具名伪装）
        # B4：档位归一——"none" 保留（上游识别，真关思考）；"off"/"disabled"
        # 上游不认识（会照常思考），直接删字段落上游默认。
        effort = request.reasoning_effort
        if effort in ("off", "disabled"):
            effort = None
        if effort:
            body["reasoning_effort"] = effort
            if effort != "none":
                body["reasoning_summary"] = "auto"  # B4：上游暴露思考的配对参数
        _neutralize_system_messages(body["messages"])
        return body

    # ─────────────────────── 错误识别钩子 ───────────────────────

    def parse_error(self, status: int, body_text: str) -> PlatformUpstreamError:
        """上游错误 → PlatformUpstreamError（status 由 routes 透传为 HTTP 状态码）。"""
        code: int | None = None
        msg = ""
        try:
            data = json.loads(body_text)
            if isinstance(data, dict):
                raw_code = data.get("code")
                if isinstance(raw_code, int) or (isinstance(raw_code, str)
                                                 and raw_code.isdigit()):
                    code = int(raw_code)
                msg = str(data.get("msg") or data.get("message") or "")
        except ValueError:
            pass
        if code == _CODE_6004_RATE_LIMIT or "频率限制" in msg or "限额" in msg:
            mapped = 429
        elif code == _CODE_11102_MODEL_MISSING:
            mapped = 404
        elif code == _CODE_11128_CHANNEL_BLOCKED:
            mapped = 400
        elif code == _CODE_11101_BAD_PARAMS and status < 400:
            mapped = 400
        else:
            mapped = status if 400 <= status <= 599 else 502
        text = msg or body_text.strip()[:300] or f"HTTP {status}"
        if code is not None:
            text = f"{text}（上游 code={code}）"
        return PlatformUpstreamError(text, status=mapped)

    # ─────────────────────── 传输 ───────────────────────

    def _headers(self, account: dict) -> dict[str, str]:
        """B1 实测（2026-10-08）桌面档头族（2api-panel 口径，与登录档位身份一致）。"""
        uid = str(account.get("uid") or "")
        headers = {
            "User-Agent": settings.wb_user_agent,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": CN_ORIGIN,
            "Referer": f"{CN_ORIGIN}/",
            "X-CodeBuddy-Request": "1",
            "Accept-Language": "zh-CN",
            "X-Agent-Purpose": "conversation",
            "X-IDE-Name": "WorkBuddy",
            "X-IDE-Type": "WorkBuddy",
            "X-IDE-Version": settings.wb_client_version,
            "X-Product": "WorkBuddy",
            "Authorization": f"Bearer {account.get('access_token') or ''}",
        }
        if uid:
            headers["X-User-Id"] = uid
            # 每号稳定派生设备头（与账号层同式；防多号设备指纹关联，B8）
            headers["X-Machine-ID"] = derive_stable_id(uid, "machine")
            headers["X-Session-ID"] = derive_stable_id(uid, "session")
        domain = str(account.get("domain") or "")
        if domain:
            headers["X-Domain"] = domain
        else:
            headers["X-No-Department-Info"] = "1"
        if account.get("enterprise_id"):
            headers["X-Enterprise-Id"] = str(account["enterprise_id"])
        else:
            headers["X-No-Enterprise-Id"] = "1"
        return headers

    def _new_client(self) -> httpx.AsyncClient:
        # trust_env=False：系统代理劫持 copilot.tencent.com 会 403（自举环境纪律）
        return httpx.AsyncClient(trust_env=False,
                                 timeout=httpx.Timeout(300.0, connect=15.0),
                                 transport=self._transport)

    async def _stream_once(self, account: dict, body: dict) -> AsyncIterator[dict]:
        """单号单次调用：HTTP 错误分类抛错（401/403 → 换号信号）。"""
        url = f"{settings.wb_api_base}{CHAT_PATH}"
        async with self._new_client() as client:
            async with client.stream("POST", url, headers=self._headers(account),
                                     json=body) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    err = self.parse_error(resp.status_code, text)
                    if resp.status_code in (401, 403):
                        raise _CredentialRejected(str(err)) from None
                    raise err
                async for line in resp.aiter_lines():
                    frame = _parse_frame(line)
                    if frame is None:
                        continue
                    if "__error__" in frame:
                        status, text = frame["__error__"]
                        raise self.parse_error(status, text)
                    yield frame

    async def _frames_with_rotation(self, request: ChatRequest) -> AsyncIterator[dict]:
        """账号池换号编排：单号 401/403 → 换下一号（每号一次）；全试完 → 503 语义。

        §4.3：全部账号凭证被拒 = 池不可用 → NoAvailableAccountError
        （routes 映射 503 + Retry-After；引导用户到供应商页重登）。
        """
        body = self.transform_request(request)
        tried: set[str] = set()
        total = len(await self._normal_accounts())
        while True:
            account = await self._pick_account(tried)
            uid = str(account.get("uid") or "")
            self._active_uid = uid
            account = await _platform_adapter.ensure_token(account)  # 复用账号层刷新
            try:
                async for frame in self._stream_once(account, body):
                    yield frame
                return
            except _CredentialRejected:
                tried.add(uid)
                logger.warning("[wb-chat] 账号 %s 凭证被拒，换号重试", uid[:8])
                if len(tried) >= total:
                    raise NoAvailableAccountError(
                        "全部 WorkBuddy 账号凭证失效，请到供应商页重新登录") from None

    # ─────────────────────── 出站事件契约 ───────────────────────

    async def stream_structured(self, request: ChatRequest) -> AsyncIterator[dict]:
        """上游 SSE → relay 事件（thinking/content/tool_call/done；同 Ta3Provider 契约）。"""
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_calls: dict[int, dict] = {}
        finish_reason: str | None = None
        usage: Usage | None = None
        raw_tail: list[str] = []

        async for frame in self._frames_with_rotation(request):
            # raw_tail 仅本地诊断（EmptyStreamError 语义）；Usage 对象经 default=str 兜底
            raw_tail = [*raw_tail[-7:],
                        json.dumps(frame, ensure_ascii=False, default=str)[:300]]
            if frame.get("__usage__") is not None:
                usage = frame["__usage__"]
            if frame.get("__finish__"):
                finish_reason = str(frame["__finish__"])
            for ev in _frame_events(frame):
                if ev["type"] == "content":
                    content_parts.append(ev["delta"])
                elif ev["type"] == "thinking":
                    thinking_parts.append(ev["delta"])
                else:
                    _merge_tool_call(tool_calls, ev)
                yield ev

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
        """非流式：内部收流聚合（B2：上游仅流式）。"""
        response = ChatResponse(content=None, tool_calls=[], finish_reason="stop",
                                usage=Usage())
        async for event in self.stream_structured(request):
            if event["type"] == "done":
                response = ChatResponse(
                    content=event["content"],
                    thinking=event["thinking"],
                    tool_calls=event["tool_calls"],
                    finish_reason=event["finish_reason"],
                    usage=event["usage"],
                    model=self._model,
                )
        return response


# ─────────────────────── 模块级工具 ───────────────────────

_cursor = 0


def _next_cursor() -> int:
    """进程内轮询游标（事件循环单线程语义下无锁安全）。"""
    global _cursor
    cur = _cursor
    _cursor = (cur + 1) % 1_000_000
    return cur


def reset_cursor() -> None:
    """测试复位。"""
    global _cursor
    _cursor = 0


def _message_payload(m: ChatMessage) -> dict:
    """ChatMessage → OpenAI 线格式（含图片块与 tool 往返字段）。"""
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
            {"id": tc.get("id") or "",
             "type": "function",
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


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(b.get("text") or "") for b in content
                         if isinstance(b, dict))
    return ""


def _neutralize_system_messages(messages: list[dict]) -> None:
    """B3：system 命中 agent 身份模式或超长 → 替换中性句（保持 str/块形状）。"""
    for msg in messages:
        if msg.get("role") != "system":
            continue
        text = _flatten_content(msg.get("content"))
        if not text:
            continue
        if len(text) > _SYSTEM_MAX_LEN or _AGENT_PATTERN.search(text):
            if isinstance(msg.get("content"), str):
                msg["content"] = NEUTRAL_SYSTEM_PROMPT
            else:
                msg["content"] = [{"type": "text", "text": NEUTRAL_SYSTEM_PROMPT}]


def _parse_frame(line: str) -> dict | None:
    """上游 SSE 行 → 精简帧；心跳/空帧/[DONE]/不可解析行 → None。

    精简帧形状：{"choices": [{index, content?, reasoning_content?, tool_calls?}],
    "__usage__": Usage|None, "__finish__": str|None, "__error__": (status, body)?}
    白名单重建：`function_call`/`refusal`/`extra_fields`/`logprobs` 等腾讯字段剥离。
    """
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
    if obj.get("error"):
        return {"__error__": (500, json.dumps(obj["error"], ensure_ascii=False)[:300])}
    code = obj.get("code")
    if code not in (None, 0):
        return {"__error__": (400, payload[:600])}  # 业务错误信封
    out: dict = {"choices": []}
    for ch in obj.get("choices") or []:
        if not isinstance(ch, dict):
            continue
        delta = ch.get("delta") or {}
        slim: dict = {"index": ch.get("index", 0)}
        if isinstance(delta.get("content"), str) and delta["content"]:
            slim["content"] = delta["content"]
        if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
            slim["reasoning_content"] = delta["reasoning_content"]
        tcs = delta.get("tool_calls")
        if isinstance(tcs, list) and tcs:
            slim["tool_calls"] = [_slim_tool_call(tc) for tc in tcs if isinstance(tc, dict)]
        if ch.get("finish_reason"):
            out["__finish__"] = str(ch["finish_reason"])
        out["choices"].append(slim)
    if isinstance(obj.get("usage"), dict):
        out["__usage__"] = _usage_from_upstream(obj["usage"])
    return out


def _slim_tool_call(tc: dict) -> dict:
    """工具调用分片白名单重建：只留 index/id/type/name/arguments。"""
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
    """上游 usage → relay Usage（B8：reasoning_tokens 取 completion_tokens_details）。"""
    details = usage.get("completion_tokens_details") or {}
    return Usage(
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        total_tokens=int(usage.get("total_tokens") or 0),
        reasoning_tokens=int(details.get("reasoning_tokens") or 0),
    )


def _frame_events(frame: dict) -> list[dict]:
    """精简帧 → relay 事件列表（content/thinking/tool_call）。"""
    events: list[dict] = []
    for ch in frame.get("choices") or []:
        if ch.get("content"):
            events.append({"type": "content", "delta": ch["content"]})
        if ch.get("reasoning_content"):
            events.append({"type": "thinking", "delta": ch["reasoning_content"]})
        for tc in ch.get("tool_calls") or []:
            events.append({"type": "tool_call", "delta": tc})
    return events


def _merge_tool_call(acc: dict[int, dict], ev: dict) -> None:
    """按 index 合并工具分片（首片带 id/name；后续分片只带 arguments 增量）。"""
    tc = ev["delta"]
    idx = int(tc.get("index") or 0)
    cur = acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
    if tc.get("id"):
        cur["id"] = str(tc["id"])
    if tc.get("name"):
        cur["name"] = str(tc["name"])
    if tc.get("arguments"):
        cur["arguments"] += str(tc["arguments"])


def _assemble_tool_calls(acc: dict[int, dict]) -> list[dict]:
    """合并表 → ChatResponse 工具调用列表（arguments 解析为对象；坏 JSON 原样）。"""
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


__all__ = ["WorkBuddyChatAdapter", "NoAvailableAccountError", "factory", "reset_cursor"]


def factory(bare_model: str, *, ctx=None, model_entry: dict | None = None,
            **_) -> WorkBuddyChatAdapter:
    """adapter 注册表工厂（relay/adapter_registry.py 的 factory 契约）。"""
    return WorkBuddyChatAdapter(bare_model, ctx=ctx, model_entry=model_entry)


# 注册到出站 adapter 注册表（导入即生效）
from relay import adapter_registry  # noqa: E402
from relay.platforms.workbuddy import catalog as _catalog  # noqa: E402

adapter_registry.register_provider("WorkBuddy", directory=_catalog.load_models,
                                   factory=factory)
