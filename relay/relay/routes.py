"""relay HTTP 路由：对外只暴露 OpenAI /v1 子集 + /v1/auth/*（GUI/CLI 用）。

- GET    /v1/models              模型列表
- GET    /v1/models/{id}         单模型（OAI 兼容，可选）
- POST   /v1/chat/completions    OpenAI 兼容；stream=true(SSE) 与 stream=false 均支持
- POST   /v1/auth/login/start    发起登录（IM 静默优先，降级浏览器 PKCE）
- GET    /v1/auth/status         登录状态轮询
- POST   /v1/auth/login/cancel   取消进行中的浏览器登录
- POST   /v1/auth/logout         退出登录
- POST   /v1/auth/sync           同步模型目录

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

from relay import auth_flow, oai_adapter, storage
from relay.config import settings
from relay.middleware import require_api_key

logger = logging.getLogger(__name__)

app = FastAPI(title="CoderProxy Relay", version="0.1.0")


# ─────────────────────────── provider 构造 ───────────────────────────

def build_provider(model: dict, model_name: str) -> Ta3Provider:
    """按目录模型条目构造 Ta3Provider（meta 对齐 vendored ta3.py 期望字段）。"""
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
    )


def _is_upstream_401(exc: Exception) -> bool:
    return isinstance(exc, RuntimeError) and "401" in str(exc)


async def _ensure_model(model_name: str) -> dict:
    """查本地模型；未找到时先同步一次目录。"""
    model = await storage.find_model(model_name)
    if model is not None:
        return model
    try:
        await auth_flow.sync_models()
    except Exception:  # noqa: BLE001（同步失败以 404 提示为准）
        pass
    model = await storage.find_model(model_name)
    if model is None:
        raise HTTPException(
            status_code=404,
            detail=f"未知模型: {model_name}（请先登录并同步模型目录 /v1/auth/sync）",
        )
    return model


async def _sse_with_retry(model_name: str, chat_request: ChatRequest,
                          include_usage: bool) -> AsyncIterator[str]:
    """流式转发；上游 401 时重新同步目录（刷新 llm- key）后重试一次。"""
    model = await _ensure_model(model_name)
    provider = build_provider(model, model_name)
    for attempt in range(2):
        try:
            async for chunk in oai_adapter.stream_openai_sse(provider, chat_request, include_usage):
                yield chunk
            return
        except RuntimeError as exc:
            if attempt == 0 and _is_upstream_401(exc):
                logger.warning("[relay] 上游 401，重新同步目录后重试一次: %s", model_name)
                try:
                    await auth_flow.sync_models()
                except Exception:  # noqa: BLE001
                    pass
                model = await storage.find_model(model_name)
                if model is None:
                    raise
                provider = build_provider(model, model_name)
                continue
            raise


async def _chat_with_retry(model_name: str, chat_request: ChatRequest):
    """非流式；上游 401 时重试一次（同 _sse_with_retry）。"""
    model = await _ensure_model(model_name)
    provider = build_provider(model, model_name)
    for attempt in range(2):
        try:
            return await provider.chat(chat_request)
        except RuntimeError as exc:
            if attempt == 0 and _is_upstream_401(exc):
                logger.warning("[relay] 上游 401，重新同步目录后重试一次: %s", model_name)
                try:
                    await auth_flow.sync_models()
                except Exception:  # noqa: BLE001
                    pass
                model = await storage.find_model(model_name)
                if model is None:
                    raise
                provider = build_provider(model, model_name)
                continue
            raise


# ─────────────────────────── OpenAI /v1 ───────────────────────────

@app.get("/v1/models", dependencies=[Depends(require_api_key)])
async def list_models():
    models = await storage.load_models()
    return {
        "object": "list",
        "data": [oai_adapter.model_to_openai(m) for m in models],
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

    stream = bool(body.get("stream", False))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    if stream:
        async def gen():
            async for chunk in _sse_with_retry(chat_request.model, chat_request, include_usage):
                yield chunk
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})
    response = await _chat_with_retry(chat_request.model, chat_request)
    model = await storage.find_model(chat_request.model)
    provider = build_provider(model, chat_request.model) if model else None
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
        "api_key": storage.relay_api_key(),
        "tool_mode": settings.tool_mode,
    }
