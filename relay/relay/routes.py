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
- GET    /v1/logs                 日志分页 + 多条件筛选（M6 SQLite 数据底座）
- GET    /v1/logs/kinds           日志类型 distinct（筛选项）
- GET    /v1/logs/models          日志模型 distinct（筛选项）
- DELETE /v1/logs                 按条件清空（无参数 = 全清）
- GET    /v1/stats                按维度聚合 token/请求数（M8 统计页）

聊天流程：
1. 解析 OpenAI 请求 → ChatRequest
2. 从本地模型目录找模型（未找到先尝试同步一次；再失败 404）
3. 构造 Ta3Provider（llm- key / apiBase / anthropic 协议），转发到牛码
4. 流式 → OpenAI SSE；非流式 → 聚合 JSON
5. 牛码 401 → 重新同步目录（刷新 llm- key）后重试一次
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import AsyncIterator

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from app.models.providers.ta3 import Ta3Provider
from app.models.schemas import ChatRequest

from relay import auth_flow, db, oai_adapter, storage, tool_disguise, tool_inventory
from relay.config import settings
from relay.middleware import require_api_key
from relay.monitor import monitor

logger = logging.getLogger(__name__)

app = FastAPI(title="CoderProxy Relay", version="0.2.0")

# 对 agent 的 OpenAI base-url 服务开关（方案B）：relay 进程常驻，仅开关 /v1 对外服务，
# 不动登录/配置/日志底座。进程级运行时状态、不持久化；进程重启自动复位为「随登录联动态」。
_service_disabled = False


async def _serving() -> bool:
    """/v1 OpenAI 服务是否可用：未被手动停止 且 已登录。"""
    if _service_disabled:
        return False
    status = await auth_flow.login_status()
    return status.get("status") == "logged_in"


@app.on_event("startup")
async def _on_startup() -> None:
    """sidecar 就绪协议：向 stdout 打一行机器可读就绪信息（GUI 侧解析端口/key）。

    flush=True：sidecar 场景 stdout 是管道（非 tty），块缓冲会吞掉就绪行，
    必须显式刷新，否则 Tauri 壳解析不到端口。
    """
    try:
        await db.ensure_initialized()  # M6：建库 + schema 自省补列（落 settings.data_dir）
    except Exception as exc:  # noqa: BLE001（日志库故障不阻断起服，仅告警）
        logger.warning("[relay] 日志库初始化失败: %s", exc)
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
        passthrough_names=ctx.passthrough_names if ctx else None,
    )


def _is_upstream_401(exc: Exception) -> bool:
    extracted = _extract_upstream_error(exc)
    if extracted is not None:
        return extracted[0] == 401
    return isinstance(exc, RuntimeError) and "401" in str(exc)


# vendored ta3.py 的上游错误形态：RuntimeError("模型请求失败 {status}：{body}")，
# 状态码与上游响应体都裹在文本里（vendored 代码不改，只能在此解析）。
_UPSTREAM_ERR_RE = re.compile(r"^模型请求失败 (\d{3})：(.*)$", re.DOTALL)


def _extract_upstream_error(exc: Exception) -> tuple[int, str] | None:
    """从上游错误 RuntimeError 拆出 (HTTP 状态码, 上游响应体)；非上游错误返回 None。"""
    m = _UPSTREAM_ERR_RE.match(str(exc))
    if not m:
        return None
    return int(m.group(1)), m.group(2).strip()


def _openai_error_payload(message: str, status: int,
                          err_type: str = "upstream_error") -> dict:
    """OpenAI 风格错误体，agent 端 SDK 能直接识别展示。"""
    return {"error": {"message": message, "type": err_type, "code": status}}


def _upstream_error_response(exc: Exception) -> JSONResponse | None:
    """上游错误 → 透传真实状态码的 OpenAI 风格错误响应；非上游错误返回 None（仍 500）。

    此前上游错误一律以 500 纯文本抛出，agent 端（如 ZCode）把 500 当可重试的
    网络错误做指数退避，403/402 这类终态错误被盲目重放到放弃；透传状态码后
    客户端按语义处理（4xx 终态直接报错，429 才重试）。
    """
    extracted = _extract_upstream_error(exc)
    if extracted is None:
        return None
    status, text = extracted
    if not 400 <= status <= 599:
        status = 502
    return JSONResponse(status_code=status,
                        content=_openai_error_payload(text[:1000], status))


async def _log_db(kind: str, **fields) -> None:
    """落库旁路：失败只告警，不影响 chat 主链路（monitor.emit 的持久化镜像）。"""
    try:
        await db.log_event(kind=kind, **fields)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[relay] 日志落库失败 kind=%s: %s", kind, exc)


def _usage_log_fields(usage) -> dict:
    """Usage → logs 各 token 列（cached/reasoning 对齐 vendored Usage 字段）。"""
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "cached_tokens": usage.cached_input_tokens,
        "reasoning_tokens": usage.reasoning_tokens,
        "total_tokens": usage.total_tokens,
    }


async def _ensure_model(model_name: str, probe: bool = False) -> dict:
    """查本地模型；未找到时先同步一次目录；白名单外模型视为不可用（M4）。

    probe=True（GUI 连通性探测，请求 body 带 `probe: true`）：跳过白名单启用判定，
    只要求模型存在于目录——探测请求必须真实到达上游，否则「先测后启用」对未启用
    模型永远 404、无法经 GUI 开关启用（需求：连通性测试-白名单豁免）。
    """
    model = await storage.find_model(model_name)
    if model is not None and (probe or await storage.is_model_enabled(model_name)):
        return model
    try:
        await auth_flow.sync_models()
    except Exception:  # noqa: BLE001（同步失败以 404 提示为准）
        pass
    model = await storage.find_model(model_name)
    if model is None or (not probe and not await storage.is_model_enabled(model_name)):
        raise HTTPException(
            status_code=404,
            detail=f"未知或未启用的模型: {model_name}"
                   "（请先登录并同步模型目录 /v1/auth/sync；"
                   "若已同步但被白名单过滤，请到配置页启用）",
        )
    return model


async def _sse_with_retry(model_name: str, chat_request: ChatRequest,
                          include_usage: bool,
                          ctx: tool_disguise.DisguiseContext,
                          usage_collector: oai_adapter.UsageCollector | None = None,
                          *, probe: bool = False,
                          ) -> AsyncIterator[str]:
    """流式转发；上游 401 时重新同步目录（刷新 llm- key）后重试一次。"""
    model = await _ensure_model(model_name, probe=probe)
    provider = build_provider(model, model_name, ctx)
    for attempt in range(2):
        if usage_collector is not None:
            usage_collector.reset()  # 每次 attempt 独立计 usage/duration，成功后取最后一次
        try:
            async for chunk in oai_adapter.stream_openai_sse(
                    provider, chat_request, include_usage,
                    usage_collector=usage_collector):
                yield chunk
            return
        except RuntimeError as exc:
            if attempt == 0 and _is_upstream_401(exc):
                logger.warning("[relay] 上游 401，重新同步目录后重试一次: %s", model_name)
                monitor.emit("auth_401_refresh", model=model_name)
                await _log_db("auth_401_refresh", model=model_name)
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
                           ctx: tool_disguise.DisguiseContext, *,
                           probe: bool = False):
    """非流式；上游 401 时重试一次（同 _sse_with_retry）。"""
    model = await _ensure_model(model_name, probe=probe)
    provider = build_provider(model, model_name, ctx)
    for attempt in range(2):
        try:
            return await provider.chat(chat_request)
        except RuntimeError as exc:
            if attempt == 0 and _is_upstream_401(exc):
                logger.warning("[relay] 上游 401，重新同步目录后重试一次: %s", model_name)
                monitor.emit("auth_401_refresh", model=model_name)
                await _log_db("auth_401_refresh", model=model_name)
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
async def list_models(all: bool = False):
    """模型目录。默认按白名单过滤（OpenAI 兼容语义）；all=1 返回完整目录（GUI 管理用）。"""
    if not all and not await _serving():
        raise HTTPException(status_code=503, detail="服务已停止，请登录并启动服务")
    models = await storage.load_models()
    if all:
        return {
            "object": "list",
            "data": [oai_adapter.model_to_openai(m) for m in models],
        }
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
    if not await _serving():
        raise HTTPException(status_code=503, detail="服务已停止，请登录并启动服务")
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from None

    # M6 duration 起点：chat_completions 收到请求时刻（monotonic，与 chat_request 落库同点）
    started_at = time.monotonic()

    # probe：GUI 连通性探测标记（body `probe: true`）。仅本地消费、不进入 ChatRequest /
    # 上游 body（oai_request_to_chat_request 手写挑字段）；语义见 _ensure_model(probe=...)。
    probe = bool(body.get("probe", False))

    chat_request = oai_adapter.oai_request_to_chat_request(body)
    if not chat_request.model:
        raise HTTPException(status_code=400, detail="缺少 model 字段")
    if not chat_request.messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")

    # agent 显式传的思考档位不在该模型上游档位内时**透传**（2026-09-06 用户拍板）：
    # 不做回退改写，档位合法性由上游网关自行处理。

    # 每模型默认思考强度（GUI 模型页配置）：agent 未显式传思考参数时按默认值下发
    oai_adapter.apply_thinking_default(
        body, chat_request, await storage.get_thinking_defaults())

    # M11：工具指纹采集（被动）。在伪装前抓 agent 声明的真实工具，落库供语义映射；
    # 跳过连通性探针（probe）请求，避免污染指纹表。
    if settings.tool_inventory_enabled and not probe:
        source_tools = body.get("tools")
        if source_tools:
            await tool_inventory.record_tools(source_tools, chat_request.model)

    # M3：按模式编排出站 tools schema，并把请求级映射表交给 provider
    ctx = tool_disguise.build_disguise_context(chat_request.tools, settings.tool_mode)
    chat_request.tools = ctx.outbound_tools

    stream = bool(body.get("stream", False))
    include_usage = bool((body.get("stream_options") or {}).get("include_usage"))

    # M4 监控：记录请求与工具伪装统计（GUI 日志面板数据源）
    monitor.emit("chat_request", model=chat_request.model, stream=stream,
                 tools=len(ctx.outbound_tools))
    # M6：chat_request 落库（tools 数走 detail 兜底，不建独立列，不入统计口径）；
    # detail 同时记录思考下发态（agent 显式传参或 relay 兜底后的最终值），
    # 供日志页/验收核对「这次请求是否开了思考、用的哪档」
    await _log_db("chat_request", model=chat_request.model, stream=1 if stream else 0,
                  detail={"tools": len(ctx.outbound_tools),
                          "thinking": chat_request.thinking,
                          "thinking_effort": chat_request.reasoning_effort})
    monitor.emit("tool_disguise", mode=ctx.mode,
                 map_hits=ctx.tool_map_hits,
                 longtail_passthrough=ctx.tool_longtail_passthrough,
                 dropped=ctx.tool_dropped)

    if stream:
        # 流式请求：SSE 建立前先做模型/白名单校验。此前校验在 gen() 内、响应头 200
        # 已发出后才执行，白名单外模型表现为「开流即断」；agent 端只见费解的断流错误，
        # 无法识别为「未知或未启用的模型」。前置后拒绝以标准 HTTP 404 返回给调用方。
        try:
            await _ensure_model(chat_request.model, probe=probe)
        except Exception as exc:  # noqa: BLE001（与 gen() 内一致：失败也落 chat_error）
            monitor.emit("chat_error", model=chat_request.model,
                         error=str(exc)[:200])
            await _log_db("chat_error", model=chat_request.model, stream=1,
                          detail={"error": str(exc)[:500]})
            raise
        collector = oai_adapter.UsageCollector(started_at)
        upstream_iter = _sse_with_retry(chat_request.model, chat_request,
                                        include_usage, ctx,
                                        usage_collector=collector,
                                        probe=probe)
        # 预拉首帧：上游错误（401/403/429/...）都发生在首帧产出前。此前错误在 gen()
        # 内才触发，响应头 200 已发出，客户端只见「开流即断」（ZCode 报 terminated
        # 并盲目重试）；预拉后可以在响应头未发时把真实状态码透传回去。
        try:
            first_chunk = await anext(upstream_iter)
        except StopAsyncIteration:
            first_chunk = None
        except Exception as exc:  # noqa: BLE001（首帧前失败也落 chat_error）
            monitor.emit("chat_error", model=chat_request.model,
                         error=str(exc)[:200])
            await _log_db("chat_error", model=chat_request.model, stream=1,
                          detail={"error": str(exc)[:500]})
            err_resp = _upstream_error_response(exc)
            if err_resp is not None:
                return err_resp
            raise

        async def gen():
            try:
                if first_chunk is not None:
                    yield first_chunk
                async for chunk in upstream_iter:
                    yield chunk
                monitor.emit("chat_done", model=chat_request.model, stream=True,
                             map_hits=ctx.tool_map_hits,
                             longtail=ctx.tool_longtail_passthrough,
                             dropped=ctx.tool_dropped)
                usage = collector.usage
                await _log_db("chat_done", model=chat_request.model, stream=1,
                              duration_ms=collector.duration_ms,
                              **(_usage_log_fields(usage) if usage else {}))
            except Exception as exc:  # noqa: BLE001（流中途断开也要记录）
                monitor.emit("chat_error", model=chat_request.model,
                             error=str(exc)[:200])
                await _log_db("chat_error", model=chat_request.model, stream=1,
                              detail={"error": str(exc)[:500]})
                # 流中途失败：响应头已发无法改状态。补一帧 OpenAI 风格错误帧再干净
                # 收尾，替代此前 raise 导致的连接硬断（客户端只见 "terminated"）。
                extracted = _extract_upstream_error(exc)
                status, text = extracted if extracted else (500, str(exc)[:500])
                payload = _openai_error_payload(text[:1000], status)
                yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache",
                                          "X-Accel-Buffering": "no"})
    try:
        response = await _chat_with_retry(chat_request.model, chat_request, ctx,
                                          probe=probe)
    except Exception as exc:  # noqa: BLE001
        monitor.emit("chat_error", model=chat_request.model, error=str(exc)[:200])
        await _log_db("chat_error", model=chat_request.model, stream=0,
                      detail={"error": str(exc)[:500]})
        err_resp = _upstream_error_response(exc)
        if err_resp is not None:
            return err_resp
        raise
    duration_ms = max(0, int((time.monotonic() - started_at) * 1000))
    monitor.emit("chat_done", model=chat_request.model, stream=False,
                 map_hits=ctx.tool_map_hits,
                 longtail=ctx.tool_longtail_passthrough,
                 dropped=ctx.tool_dropped)
    await _log_db("chat_done", model=chat_request.model, stream=0,
                  duration_ms=duration_ms, **_usage_log_fields(response.usage))
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
    global _service_disabled
    await auth_flow.logout()
    # 登出后复位服务开关：下次登录时对 agent 的服务自动启动
    _service_disabled = False
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
        "thinking_defaults": await storage.get_thinking_defaults(),
        "thinking_unset_mode": await storage.get_thinking_unset_mode(),
    }


@app.post("/v1/auth/config", dependencies=[Depends(require_api_key)])
async def auth_config_update(request: Request):
    """GUI 配置页写入：api_key / tool_mode / model_whitelist（部分更新）。

    写路径固定：① 全字段校验（任一项非法即 400，不进入任何写）→
    ② 逐字段落盘（storage.save_*）→ ③ 落盘成功后同步内存运行值 →
    ④ 返回完整 config（= 前端整体替换 store 的唯一回执）。
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail="请求体必须是 JSON") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="请求体必须是对象")

    # ── ① 全字段先校验，避免改了一半才撞上非法字段 ──
    api_key = body.get("api_key")
    if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
        raise HTTPException(status_code=400, detail="访问密钥不能为空")

    tool_mode = body.get("tool_mode")
    if tool_mode is not None and tool_mode not in tool_disguise.VALID_MODES:
        raise HTTPException(status_code=400, detail="无效的工具映射")

    model_whitelist = body.get("model_whitelist")
    if model_whitelist is not None and not isinstance(model_whitelist, list):
        raise HTTPException(status_code=400, detail="模型列表格式不正确")

    thinking_defaults = body.get("thinking_defaults")
    if thinking_defaults is not None and (
        not isinstance(thinking_defaults, dict)
        or not all(isinstance(k, str) and storage.valid_thinking_effort(v)
                   for k, v in thinking_defaults.items())
    ):
        raise HTTPException(status_code=400, detail="思考强度默认值格式不正确")

    thinking_unset_mode = body.get("thinking_unset_mode")
    if thinking_unset_mode is not None and (
        thinking_unset_mode not in storage.VALID_THINKING_UNSET_MODES
    ):
        raise HTTPException(status_code=400, detail="无效的思考兜底策略")

    port = body.get("port")
    if port is not None and (not isinstance(port, int) or not (1 <= port <= 65535)):
        raise HTTPException(status_code=400, detail="port 必须是 1-65535 的整数")

    # ── ②③ 逐字段落盘；save_* 内部落盘成功后同步 settings ──
    if api_key is not None:
        await storage.save_api_key(api_key.strip())
    if body.get("regenerate_api_key") is True:
        # 重置密钥：生成新 key 落盘（作废旧值；已连接的 agent 需改用新 key）
        await storage.regenerate_api_key()
    if tool_mode is not None:
        await storage.save_tool_mode(tool_mode)
        settings.tool_mode = tool_mode
    if model_whitelist is not None:
        await storage.set_model_whitelist(model_whitelist)
    if thinking_defaults is not None:
        await storage.save_thinking_defaults(thinking_defaults)
    if thinking_unset_mode is not None:
        await storage.save_thinking_unset_mode(thinking_unset_mode)
    if port is not None:
        await storage.save_port(port)

    return await auth_config()


# ─────────────────────────── /v1/service（对 agent 的 OpenAI 服务开关，方案B）───────────────────────────

@app.get("/v1/service", dependencies=[Depends(require_api_key)])
async def service_status():
    """查询对 agent 的 /v1 服务是否运行中（逻辑：已登录 且 未手动停止）。"""
    return {"enabled": await _serving()}


@app.post("/v1/service/disable", dependencies=[Depends(require_api_key)])
async def service_disable():
    """停止对 agent 的 /v1 服务：仅关 agent 入口，登录/配置/日志底座仍可用。"""
    global _service_disabled
    _service_disabled = True
    return {"enabled": False, "status": "stopped"}


@app.post("/v1/service/enable", dependencies=[Depends(require_api_key)])
async def service_enable():
    """重新启用对 agent 的 /v1 服务（需已登录，否则仍保持关闭）。"""
    global _service_disabled
    _service_disabled = False
    return {"enabled": await _serving(), "status": "started"}


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


# ─────────────────────────── /v1/logs/*（M6 数据底座：SQLite 持久化日志）───────────────────────────

_STATS_GROUP_BY = {"model", "kind", "day", "hour"}


@app.get("/v1/stats", dependencies=[Depends(require_api_key)])
async def logs_stats(group_by: str, model: str | None = None, kind: str | None = None,
                     time_from: str | None = None, time_to: str | None = None):
    """按维度聚合 token/请求数（M8 统计页）。

    - group_by（必填）：model | kind | day | hour。
    - model / kind / time_from / time_to：与 /v1/logs 同口径的过滤（可组合）。
    - 返回 { rows, total }；rows 每组一个 key + 汇总字段，total 为未分组总量。
    """
    if group_by not in _STATS_GROUP_BY:
        raise HTTPException(
            status_code=400,
            detail=f"group_by 必须是 {', '.join(sorted(_STATS_GROUP_BY))} 之一")
    rows, total = await db.query_stats(
        group_by=group_by, model=model, kind=kind,
        time_from=time_from, time_to=time_to)
    return {"rows": rows, "total": total}


@app.get("/v1/logs", dependencies=[Depends(require_api_key)])
async def logs_list(limit: int = 50, offset: int = 0, kind: str | None = None,
                    model: str | None = None, time_from: str | None = None,
                    time_to: str | None = None):
    """分页 + 多条件筛选（kind/model/time_from/time_to 可组合）。

    返回 { rows, total, kinds, models }；rows 按 id 倒序（最新在前），
    kinds/models 供前端筛选项下拉（与 /v1/logs/kinds|models 同源）。
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    rows, total = await db.query_logs(
        limit=limit, offset=offset, kind=kind, model=model,
        time_from=time_from, time_to=time_to)
    kinds, models = await db.distinct_kinds(), await db.distinct_models()
    return {"rows": rows, "total": total, "kinds": kinds, "models": models}


@app.get("/v1/logs/kinds", dependencies=[Depends(require_api_key)])
async def logs_kinds():
    """日志类型 distinct 列表（筛选项下拉）。"""
    return {"kinds": await db.distinct_kinds()}


@app.get("/v1/logs/models", dependencies=[Depends(require_api_key)])
async def logs_models():
    """出现过日志的模型 distinct 列表（筛选项下拉）。"""
    return {"models": await db.distinct_models()}


@app.delete("/v1/logs", dependencies=[Depends(require_api_key)])
async def logs_clear(kind: str | None = None, model: str | None = None,
                     time_from: str | None = None, time_to: str | None = None):
    """按条件清空日志（无参数 = 全清）；返回删除行数。"""
    deleted = await db.clear_logs(kind=kind, model=model,
                                  time_from=time_from, time_to=time_to)
    return {"status": "cleared", "deleted": deleted}
