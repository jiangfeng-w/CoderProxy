"""relay HTTP 路由：对外只暴露 OpenAI /v1 子集 + /v1/auth/*（GUI/CLI 用）。

- GET    /v1/models              模型列表
- GET    /v1/models/{id}         单模型（OAI 兼容，可选）
- POST   /v1/chat/completions    OpenAI 兼容；stream=true(SSE) 与 stream=false 均支持
- POST   /v1/auth/login/start    发起登录（IM 静默优先，降级浏览器 PKCE）
- GET    /v1/auth/status         登录状态轮询
- POST   /v1/auth/login/cancel   取消进行中的浏览器登录
- POST   /v1/auth/logout         退出登录
- POST   /v1/auth/sync           同步模型目录
- GET    /v1/auth/config         中转配置展示（GUI）
- POST   /v1/auth/config         写入 api_key / tool_mode / model_whitelist / regenerate_api_key
- GET    /v1/monitor/stats        统计计数快照（含工具映射聚合 tool_*）
- GET    /v1/monitor/events       增量事件拉取（after_id）
- POST   /v1/monitor/clear        清空事件与统计（GUI 日志页）

聊天流程：
1. 解析 OpenAI 请求 → ChatRequest
2. 从本地模型目录找模型（未找到先尝试同步一次；再失败 404）
3. 构造 Ta3Provider（llm- key / apiBase / anthropic 协议），转发到牛码
4. 流式 → OpenAI SSE；非流式 → 聚合 JSON
5. 牛码 401 → 重新同步目录（刷新 llm- key）后重试一次
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.models.providers.ta3 import Ta3Provider
from app.models.schemas import ChatRequest

from relay import auth_flow, oai_adapter, storage, tool_disguise
from relay.config import settings
from relay.middleware import require_api_key
from relay.monitor import monitor

logger = logging.getLogger(__name__)

app = FastAPI(title="CoderProxy Relay", version="0.2.0")


@app.on_event("startup")
async def _on_startup() -> None:
    """sidecar 就绪协议：向 stdout 打一行机器可读就绪信息（GUI 侧解析端口/key）。

    flush=True：sidecar 场景 stdout 是管道（非 tty），块缓冲会吞掉就绪行，
    必须显式刷新，否则 Tauri 壳解析不到端口。
    """
    print(f"[relay-ready] port={settings.relay_port} "
          f"api_key={settings.relay_api_key or storage.relay_api_key()} "
          f"tool_mode={settings.tool_mode}", flush=True)


# ─────────────────────────── provider 构造 ───────────────────────────

def build_provider(model: dict, model_name: str,
                   ctx: tool_disguise.DisguiseContext | None = None) -> Ta3Provider:
    """按目录模型条目构造 Ta3Provider（meta 对齐 vendored ta3.py 期望字段）。

    M3：传入 DisguiseContext → provider 按请求级映射做历史伪装与入站还原，
    tools 已预伪装（tools_pre_disguised），不再二次 disguise_tools。
    """
    meta = {
        "anthropic": bool(model.get("anthropic")),
        "completionOptions": model.get("completion_options") or {},
        "requestHeaders": model.get("request_headers") or {},
        "provider": model.get("provider") or "",
        "title": model.get("title") or model_name,
    }
    return Ta3Provider(
        api_key=model.get("api_key") or "",
        base_url=model.get("base_url") or settings.ta3_api_base,
        model=model_name,
        meta=meta,
        tool_mode=ctx.mode if ctx else settings.tool_mode,
        disguise_map=ctx.disguise_map if ctx else None,
        restore_map=ctx.restore_map if ctx else None,
        args_to_ta3=ctx.args_to_ta3 if ctx else None,
        args_from_ta3=ctx.args_from_ta3 if ctx else None,
        tools_pre_disguised=bool(ctx),
    )


def _is_upstream_401(exc: Exception) -> bool:
    return isinstance(exc, RuntimeError) and "401" in str(exc)


async def _ensure_model(model_name: str) -> dict:
    """查本地模型；未找到时先同步一次目录；白名单外模型视为不可用（M4）。"""
    model = await storage.find_model(model_name)
    if model is not None and await storage.is_model_enabled(model_name):
        return model
    try:
        await auth_flow.sync_models()
    except Exception:  # noqa: BLE001（同步失败以 404 提示为准）
        pass
    model = await storage.find_model(model_name)
    if model is None or not await storage.is_model_enabled(model_name):
        raise HTTPException(
            status_code=404,
            detail=f"未知或未启用的模型: {model_name}"
                   "（请先登录并同步模型目录 /v1/auth/sync；"
                   "若已同步但被白名单过滤，请到配置页启用）",
        )
    return model


async def _sse_with_retry(model_name: str, chat_request: ChatRequest,
                          include_usage: bool,
                          ctx: tool_disguise.DisguiseContext) -> AsyncIterator[str]:
    """流式转发；上游 401 时重新同步目录（刷新 llm- key）后重试一次。"""
    model = await _ensure_model(model_name)
    provider = build_provider(model, model_name, ctx)
    for attempt in range(2):
        try:
            async for chunk in oai_adapter.stream_openai_sse(provider, chat_request, include_usage):
                yield chunk
            return
        except RuntimeError as exc:
            if attempt == 0 and _is_upstream_401(exc):
                logger.warning("[relay] 上游 401，重新同步目录后重试一次: %s", model_name)
                monitor.emit("auth_401_refresh", model=model_name)
                try:
                    await auth_flow.sync_models()
                except Exception:  # noqa: BLE001
                    pass
                model = await storage.find_model(model_name)
                if model is None:
                    raise
                provider = build_provider(model, model_name, ctx)
                continue
            raise


async def _chat_with_retry(model_name: str, chat_request: ChatRequest,
                           ctx: tool_disguise.DisguiseContext):
    """非流式；上游 401 时重试一次（同 _sse_with_retry）。"""
    model = await _ensure_model(model_name)
    provider = build_provider(model, model_name, ctx)
    for attempt in range(2):
        try:
            return await provider.chat(chat_request)
        except RuntimeError as exc:
            if attempt == 0 and _is_upstream_401(exc):
                logger.warning("[relay] 上游 401，重新同步目录后重试一次: %s", model_name)
                monitor.emit("auth_401_refresh", model=model_name)
                try:
                    await auth_flow.sync_models()
                except Exception:  # noqa: BLE001
                    pass
                model = await storage.find_model(model_name)
                if model is None:
                    raise
                provider = build_provider(model, model_name, ctx)
                continue
            raise


# ─────────────────────────── OpenAI /v1 ───────────────────────────

@app.get("/v1/models", dependencies=[Depends(require_api_key)])
async def list_models():
    models = await storage.load_models()
    wl = await storage.get_model_whitelist()
    if wl == [storage.DISABLE_ALL]:
        data = []
    elif wl:
        data = [m for m in models if m.get("name") in wl]
    else:
        data = models
    return {
        "object": "list",
        "data": [oai_adapter.model_to_openai(m) for m in data],
    }


@app.get("/v1/models/{model_id}", dependencies=[Depends(require_api_key)])
async def get_model(model_id: str):
    model = await storage.find_model(model_id)
    if model is None:
        raise HTTPException(status_code=404, detail="Model not found")
    return oai_adapter.model_to_openai(model)


@app.post("/v1/chat/completions", dependencies=[Depends(require_api_key)])
async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from None

    chat_request = oai_adapter.oai_request_to_chat_request(body)
    if not chat_request.model:
        raise HTTPException(status_code=400, detail="缺少 model 字段")
    if not chat_request.messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")

    # M3：按模式编排出站 tools schema，并把请求级映射表交给 provider
    ctx = tool_disguise.build_disguise_context(chat_request.tools, settings.tool_mode)
    chat_request.tools = ctx.outbound_tools

    stream = bool(body.get("stream", False))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    # M4 监控：记录请求与工具伪装统计（GUI 日志面板数据源）
    monitor.emit("chat_request", model=chat_request.model, stream=stream,
                 tools=len(ctx.outbound_tools))
    monitor.emit("tool_disguise", mode=ctx.mode,
                 map_hits=ctx.tool_map_hits,
                 longtail_passthrough=ctx.tool_longtail_passthrough,
                 dropped=ctx.tool_dropped)

    if stream:
        async def gen():
            try:
                async for chunk in _sse_with_retry(chat_request.model, chat_request, include_usage, ctx):
                    yield chunk
                monitor.emit("chat_done", model=chat_request.model, stream=True,
                             map_hits=ctx.tool_map_hits,
                             longtail=ctx.tool_longtail_passthrough,
                             dropped=ctx.tool_dropped)
            except Exception as exc:  # noqa: BLE001（流中途断开也要记录）
                monitor.emit("chat_error", model=chat_request.model,
                             error=str(exc)[:200])
                raise
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})
    try:
        response = await _chat_with_retry(chat_request.model, chat_request, ctx)
    except Exception as exc:  # noqa: BLE001
        monitor.emit("chat_error", model=chat_request.model, error=str(exc)[:200])
        raise
    monitor.emit("chat_done", model=chat_request.model, stream=False,
                 map_hits=ctx.tool_map_hits,
                 longtail=ctx.tool_longtail_passthrough,
                 dropped=ctx.tool_dropped)
    model = await storage.find_model(chat_request.model)
    provider = build_provider(model, chat_request.model, ctx) if model else None
    return JSONResponse(oai_adapter.chat_response_to_openai(provider, chat_request, response))


# ─────────────────────────── /v1/auth/* ───────────────────────────

@app.post("/v1/auth/login/start")
async def auth_login_start():
    """发起登录：IM 静默优先（返回 logged_in），否则 pending（需浏览器授权）。"""
    try:
        result = await auth_flow.start_login()
    except Exception as e:  # noqa: BLE001
        logger.warning("[relay] 登录启动失败: %s", e)
        raise HTTPException(status_code=500, detail=f"登录启动失败：{e}") from e
    if result.get("status") == "pending":
        return {
            "status": "pending",
            "authorize_url": result.get("authorize_url"),
            "expires_in": result.get("expires_in"),
        }
    return result


@app.get("/v1/auth/status")
async def auth_login_status():
    return await auth_flow.login_status()


@app.post("/v1/auth/login/cancel")
async def auth_login_cancel():
    await auth_flow.cancel_login()
    return {"status": "cancelled"}


@app.post("/v1/auth/logout")
async def auth_logout():
    await auth_flow.logout()
    return {"status": "logged_out"}


@app.post("/v1/auth/sync")
async def auth_sync():
    """同步模型目录（需已登录）。"""
    try:
        models = await auth_flow.sync_models()
    except Exception as e:  # noqa: BLE001
        logger.warning("[relay] 目录同步失败: %s", e)
        raise HTTPException(status_code=500, detail=f"目录同步失败：{e}") from e
    return {"status": "ok", "models": [m.get("name") for m in models]}


@app.get("/v1/auth/config")
async def auth_config():
    """GUI/CLI 展示用：中转配置（不泄漏牛码 token）。"""
    return {
        "base_url": f"http://{settings.relay_host}:{settings.relay_port}/v1",
        "host": settings.relay_host,
        "port": settings.relay_port,
        "api_key": storage.relay_api_key(),
        "tool_mode": settings.tool_mode,
        "model_whitelist": await storage.get_model_whitelist(),
    }


@app.post("/v1/auth/config", dependencies=[Depends(require_api_key)])
async def auth_config_update(request: Request):
    """GUI 配置页写入：api_key / tool_mode / model_whitelist（部分更新）。"""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")

    api_key = body.get("api_key")
    if api_key is not None:
        if not isinstance(api_key, str) or not api_key.strip():
            raise HTTPException(status_code=400, detail="api_key 不能为空")
        await storage.save_api_key(api_key.strip())

    if body.get("regenerate_api_key") is True:
        # 重置密钥：生成新 key 落盘（作废旧值；已连接的 agent 需改用新 key）
        await storage.regenerate_api_key()

    tool_mode = body.get("tool_mode")
    if tool_mode is not None:
        if tool_mode not in tool_disguise.VALID_MODES:
            raise HTTPException(status_code=400,
                                detail=f"tool_mode 必须是 {list(tool_disguise.VALID_MODES)}")
        settings.tool_mode = tool_mode

    model_whitelist = body.get("model_whitelist")
    if model_whitelist is not None:
        if not isinstance(model_whitelist, list):
            raise HTTPException(status_code=400, detail="model_whitelist 必须是数组")
        await storage.set_model_whitelist(model_whitelist)

    return await auth_config()


# ─────────────────────────── /v1/monitor/*（M4 GUI 日志面板）───────────────────────────

@app.get("/v1/monitor/stats", dependencies=[Depends(require_api_key)])
async def monitor_stats():
    """统计计数快照（按事件 kind 累加）。"""
    return monitor.stats()


@app.get("/v1/monitor/events", dependencies=[Depends(require_api_key)])
async def monitor_events(limit: int = 200, after_id: int = 0):
    """增量事件拉取：after_id 之后的 events + stats 快照。"""
    return monitor.events(limit=limit, after_id=after_id)


@app.post("/v1/monitor/clear", dependencies=[Depends(require_api_key)])
async def monitor_clear():
    """清空事件缓冲与统计计数（GUI 日志页「清空」按钮）。"""
    monitor.clear()
    return {"status": "cleared"}
