"""平台管理端点（/v1/platforms/*）：注册表分发 + 统一错误映射。

错误映射（spec 定稿）：
- PlatformAuthError(kind=login_required/refresh_failed) → 401 {code:"login_required"}
- PlatformAuthError(kind=network)                      → 504
- PlatformUpstreamError / PlatformError                → 502
- 未知 platform                                        → 404

账号响应一律经 AccountView（不含任何 token）；全部端点 require_api_key（M6+ 管理端点惯例）。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException

from relay.middleware import require_api_key
from relay.platforms import PLATFORMS
from relay.platforms.base import (
    PlatformAdapter,
    PlatformAuthError,
    PlatformError,
    PlatformUpstreamError,
)

router = APIRouter(prefix="/v1/platforms", dependencies=[Depends(require_api_key)])


def _platform_or_404(platform: str) -> PlatformAdapter:
    adapter = PLATFORMS.get(platform)
    if adapter is None:
        raise HTTPException(status_code=404, detail=f"未知平台: {platform}")
    return adapter


async def _dispatch(platform: str, handler: Callable[[PlatformAdapter], Awaitable[dict]]) -> dict:
    """按注册表分发执行 handler(adapter) 并做统一错误映射。"""
    adapter = _platform_or_404(platform)
    try:
        return await handler(adapter)
    except HTTPException:
        raise
    except PlatformAuthError as exc:
        if exc.kind == "network":
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        raise HTTPException(status_code=401,
                            detail={"code": "login_required",
                                    "message": str(exc)}) from exc
    except PlatformUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PlatformError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get("")
async def list_platforms():
    """平台注册表（前端供应商页 tab 动态性的依据）。"""
    return {"platforms": [PLATFORMS[pid].info().__dict__ for pid in sorted(PLATFORMS)]}


@router.get("/{platform}/accounts")
async def list_accounts(platform: str):
    async def _run(a: PlatformAdapter) -> dict:
        views = await a.accounts()
        return {"accounts": [v.to_dict() for v in views]}
    return await _dispatch(platform, _run)


@router.post("/{platform}/login/start")
async def login_start(platform: str):
    return await _dispatch(platform, lambda a: a.start_login())


@router.get("/{platform}/login/status")
async def login_status(platform: str, login_id: str):
    return await _dispatch(platform, lambda a: a.login_status(login_id))


@router.post("/{platform}/login/cancel")
async def login_cancel(platform: str, login_id: str = ""):
    async def _run(a: PlatformAdapter) -> dict:
        await a.cancel_login(login_id)
        return {"status": "cancelled"}
    return await _dispatch(platform, _run)


@router.delete("/{platform}/accounts/{uid}")
async def delete_account(platform: str, uid: str):
    async def _run(a: PlatformAdapter) -> dict:
        deleted = await a.delete_account(uid)
        if not deleted:
            raise HTTPException(status_code=404, detail="账号不存在")
        return {"status": "deleted"}
    return await _dispatch(platform, _run)


@router.post("/{platform}/accounts/{uid}/checkin")
async def checkin(platform: str, uid: str):
    async def _run(a: PlatformAdapter) -> dict:
        result, view = await a.checkin(uid)
        return {"result": result.__dict__, "account": view.to_dict()}
    return await _dispatch(platform, _run)


@router.get("/{platform}/accounts/{uid}/checkin-status")
async def checkin_status(platform: str, uid: str):
    return await _dispatch(platform, lambda a: a.refresh_checkin_status(uid))


@router.post("/{platform}/accounts/{uid}/quota")
async def refresh_quota(platform: str, uid: str):
    return await _dispatch(platform, lambda a: a.refresh_quota(uid))
