"""ta3 登录态存储与会话管理（relay 适配版）。

接口对齐 chatcoder 上游 session.py（save_auth/load_auth/clear_auth/get_access_token/
get_auth_row/ensure_token/refresh_access_token/mark_login_required/Ta3AuthError），
但持久化改为本地 JSON 文件（relay.storage，M2 需求：token 落本地文件，非 DB）。

db 参数仅为兼容 vendored oauth.py/catalog.py 的调用签名保留，实际被忽略
（上游是 SQLAlchemy session，relay 无 DB）。oauth.py 中对 db 的 commit/flush
调用由调用方传入 no-op db 对象兜底（见 relay/auth_flow.py 的 noop_db）。

行为对齐上游：
- ensure_token：业务请求 401 时自动 refresh（带 in-flight 锁防并发 stampede，
  对齐参考项目 authService.ts:787-792 refreshIfNeeded）
- refresh_token 失效（invalid_grant）→ 清会话并抛错，调用方提示重新登录
"""
from __future__ import annotations

import asyncio
import logging

import httpx

from app.core.config import settings
from relay.storage import (
    Ta3AuthError,
    clear_auth,
    get_access_token,
    get_auth_row,
    load_auth,
    save_auth,
)

logger = logging.getLogger(__name__)

OAUTH_TOKEN_PATH = "/api/oauth/token"
YINHAI_OAUTH_CLIENT_ID = "ide-vscode"  # 复用服务端已注册 ClientRegistry（参考项目 auth/settings.ts:44）
_TOKEN_TIMEOUT = 10.0

# relay 单账号，provider_id 恒为 1
PROVIDER_ID = 1

# refresh 续期 in-flight 锁（按 provider_id），防多个 401 并发触发 stampede
_refresh_locks: dict[int, asyncio.Lock] = {}


def _get_refresh_lock(provider_id: int) -> asyncio.Lock:
    lock = _refresh_locks.get(provider_id)
    if lock is None:
        lock = asyncio.Lock()
        _refresh_locks[provider_id] = lock
    return lock


async def refresh_access_token(api_base: str, refresh_token: str) -> dict:
    """POST {api_base}/api/oauth/token（grant_type=refresh_token），refresh 轮转。

    返回 {accessToken, refreshToken, loginId, userId, orgId, userName}。
    失败（invalid_grant）抛 Ta3AuthError(kind='login_required')。
    """
    url = f"{api_base.rstrip('/')}{OAUTH_TOKEN_PATH}"
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": YINHAI_OAUTH_CLIENT_ID,
    }
    try:
        async with httpx.AsyncClient(timeout=_TOKEN_TIMEOUT,
                                     headers={"User-Agent": settings.ta3_user_agent}) as client:
            resp = await client.post(url, data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    except httpx.HTTPError as e:
        raise Ta3AuthError(f"OAuth token 请求失败：{url}", "network") from e

    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    access_token = body.get("access_token") or body.get("accessToken") or body.get("token")
    if not resp.is_success or not access_token:
        code = body.get("error") or body.get("error_code") or f"http_{resp.status_code}"
        if code == "invalid_grant":
            raise Ta3AuthError("登录已过期（refresh_token 失效），请重新登录", "login_required")
        raise Ta3AuthError(f"OAuth token 刷新失败：{code}", "refresh_failed")
    return {
        "accessToken": access_token,
        "refreshToken": body.get("refresh_token") or body.get("refreshToken") or "",
        "loginId": body.get("login_id") or body.get("loginId"),
        "userId": body.get("user_id") or body.get("userId"),
        "orgId": body.get("org_id") or body.get("orgId"),
        "userName": body.get("user_name") or body.get("userName"),
    }


async def ensure_token(db=None, provider_id: int = PROVIDER_ID, api_base: str = "") -> str:
    """返回可用 access_token；无 refresh_token 或刷新失败时抛 Ta3AuthError。"""
    row = await load_auth(provider_id)
    if row is None or not row.access_token:
        raise Ta3AuthError("请先登录 Ta+3 账号", "login_required")
    if not row.refresh_token:
        return row.access_token  # IM 静默登录路径无 refresh，直接返回（由调用方在 401 时引导重登）

    lock = _get_refresh_lock(provider_id)
    async with lock:
        # 双检：等待锁期间可能已被其它协程刷新
        row = await load_auth(provider_id)
        if row is None or not row.access_token:
            raise Ta3AuthError("请先登录 Ta+3 账号", "login_required")
        try:
            result = await refresh_access_token(api_base, row.refresh_token)
        except Ta3AuthError as e:
            if e.kind == "login_required":
                await clear_auth(provider_id)
            raise
        account = dict(row.account or {})
        for key, src in (("id", "loginId"), ("label", "userName")):
            if result.get(src):
                account[key] = result[src]
        await save_auth(
            provider_id,
            access_token=result["accessToken"],
            refresh_token=result.get("refreshToken") or None,
            account=account,
        )
        logger.info("[ta3] provider=%s token 已刷新", provider_id)
        return result["accessToken"]


async def mark_login_required(db=None, provider_id: int = PROVIDER_ID) -> None:
    """业务请求返回 401 且无 refresh_token 可用时，清会话要求重登。"""
    await clear_auth(provider_id)


__all__ = [
    "Ta3AuthError",
    "PROVIDER_ID",
    "load_auth",
    "get_auth_row",
    "save_auth",
    "clear_auth",
    "get_access_token",
    "ensure_token",
    "refresh_access_token",
    "mark_login_required",
]
