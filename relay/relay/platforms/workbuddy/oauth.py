"""WorkBuddy 设备授权登录流：start → 浏览器授权 → relay 后台 task 轮询 → status 查询。

与 ta3 in-flight task 模式（app/auth/ta3/oauth.py）对齐，升级为 login_id 多槽并存
（多账号可并行登录）：进程内状态，重启即失效——登录本就是短生命周期操作。

- start_login：POST {api_base}/v2/plugin/auth/state?platform=CLI → {state, authUrl}；
  生成 login_id 起后台轮询 task（LOGIN_TIMEOUT_S 强制超时 + POLL_INTERVAL_S 间隔）
- 轮询 task：GET {api_base}/v2/plugin/auth/token?state=；code 0/200 且 accessToken 非空
  → 拉 login/account 归档账号（store.upsert preserve：重登录保留 checkin/quota 缓存）
- login_status：查询槽（完成即弹出，顺带清理过期槽防泄漏）
- refresh_token：POST {web_base}/v2/plugin/auth/token/refresh（X-Refresh-Token 头
  只允许出现在此请求）；上游 401 → login_required（引导重登）
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field

from relay.config import settings
from relay.platforms import store
from relay.platforms.base import PlatformAuthError, PlatformUpstreamError
from relay.platforms.workbuddy.client import (
    WB_API_PREFIX,
    WbClient,
    parse_envelope,
    plugin_headers,
    refresh_headers,
)

logger = logging.getLogger(__name__)

PLATFORM_ID = "workbuddy"

# OAuth platform 档位：官方 WorkBuddy 桌面客户端身份（cockpit-tools 18.7k★ 的
# workbuddy_oauth.rs:9 与 workbuddy-switch 1.0k★ 的 variant.rs 同用此值；2api 谱系
# 的 workbuddy-cockpit/workbuddy2api-panel 用 CLI——授权域语义不同，不采）。
# 提为常量：上游若调整档位命名，改此处即可（后续可升级为 env 可覆盖配置项）。
OAUTH_PLATFORM = "workbuddy"

LOGIN_TIMEOUT_S = 600  # spec：轮询 600s 强制超时
POLL_INTERVAL_S = 1.5  # 对齐参考实现 1500ms（测试 monkeypatch 加速）

_client = WbClient()


@dataclass
class _LoginSlot:
    """一个进行中的登录槽：task 结果经槽状态暴露给 login_status 轮询。"""

    login_id: str
    state: str
    authorize_url: str
    expires_at: float = 0.0
    status: str = "pending"  # pending | success | failed
    error: str = ""
    account_uid: str = ""
    task: asyncio.Task | None = field(default=None, repr=False)


# login_id → in-flight 槽
_slots: dict[str, _LoginSlot] = {}


def _expiry_ms(data: dict) -> int:
    """token 过期时刻归一毫秒：expiresAt 秒/毫秒双口径；否则 expiresIn 秒偏移。"""
    raw = data.get("expiresAt") or data.get("expires_at")
    if raw:
        try:
            return store.normalize_expires_ms(int(raw))
        except (TypeError, ValueError):
            pass
    expires_in = data.get("expiresIn") or data.get("expires_in")
    try:
        return int(time.time() * 1000) + int(expires_in) * 1000
    except (TypeError, ValueError):
        return 0


def _will_expire_soon(account: dict, margin_ms: int = 60_000) -> bool:
    """expires_at 距今不足 margin 或缺失 → 视为将过期（ensure_token 刷新判据）。"""
    expires_at = account.get("expires_at") or 0
    return expires_at <= (time.time() * 1000 + margin_ms)


async def start_login() -> dict:
    """发起设备授权：返回 {login_id, authorize_url, expires_in}，前端开浏览器授权。"""
    url = f"{settings.wb_api_base}{WB_API_PREFIX}/auth/state?platform={OAUTH_PLATFORM}"
    body = await _client.request_json("POST", url, headers=plugin_headers(), json_body={})
    data = parse_envelope(body, context="auth/state")
    state = data.get("state")
    if not state:
        raise PlatformUpstreamError("auth/state 响应缺少 state", status=502)
    auth_url = (data.get("authUrl") or data.get("auth_url") or data.get("url")
                or f"{settings.wb_api_base}/login?state={state}")

    login_id = f"wb_{secrets.token_hex(8)}"
    slot = _LoginSlot(login_id=login_id, state=str(state), authorize_url=auth_url,
                      expires_at=time.monotonic() + LOGIN_TIMEOUT_S)
    slot.task = asyncio.create_task(_poll_token(slot))
    _slots[login_id] = slot
    return {"login_id": login_id, "authorize_url": auth_url, "expires_in": LOGIN_TIMEOUT_S}


async def _poll_token(slot: _LoginSlot) -> None:
    """后台轮询 auth/token；超时/取消置 failed；成功归档账号置 success。"""
    token_url = f"{settings.wb_api_base}{WB_API_PREFIX}/auth/token?state={slot.state}"
    try:
        while time.monotonic() < slot.expires_at:
            await asyncio.sleep(POLL_INTERVAL_S)
            try:
                body = await _client.request_json("GET", token_url,
                                                  headers=plugin_headers())
            except (PlatformAuthError, PlatformUpstreamError):
                continue  # 未授权期间上游回非成功形态/网络抖动：继续轮询到超时
            code = body.get("code")
            data = body.get("data") if isinstance(body.get("data"), dict) else {}
            access = str(data.get("accessToken") or data.get("access_token") or "")
            if code in (0, 200) and access:
                account = await _archive_account(slot.state, access, data)
                slot.account_uid = account["uid"]
                slot.status = "success"
                return
        slot.status = "failed"
        slot.error = "登录超时，请重试"
    except asyncio.CancelledError:
        slot.status = "failed"
        slot.error = "登录已取消"
    except Exception as exc:  # noqa: BLE001
        # 后台 task 兜底：任何异常进槽供 status 读取，不让 task 静默死亡
        slot.status = "failed"
        slot.error = str(exc)[:300]
        logger.warning("[workbuddy] 登录轮询异常: %s", exc)


async def _archive_account(state: str, access_token: str, token_data: dict) -> dict:
    """拉取账号信息并入库（uid 是后续所有操作的键）。"""
    account_url = f"{settings.wb_api_base}{WB_API_PREFIX}/login/account?state={state}"
    domain = str(token_data.get("domain") or "")
    info: dict = {}
    try:
        body = await _client.request_json(
            "GET", account_url,
            headers=plugin_headers(access_token=access_token, domain=domain))
        info = parse_envelope(body, context="login/account")
    except (PlatformAuthError, PlatformUpstreamError) as exc:
        logger.warning("[workbuddy] 拉取账号信息失败（降级用 token 载荷）: %s", exc)

    uid = str(info.get("uid") or f"wb_{state[:12]}")
    entry = {
        "uid": uid,
        "nickname": str(info.get("nickname") or ""),
        "email": str(info.get("email") or ""),
        "enterprise_id": str(info.get("enterpriseId") or ""),
        "domain": domain,
        "access_token": access_token,
        "refresh_token": str(token_data.get("refreshToken")
                             or token_data.get("refresh_token") or ""),
        "expires_at": _expiry_ms(token_data),
        "status": "normal",
    }
    account = await store.upsert_account(PLATFORM_ID, entry, preserve=True)
    await _prefetch_caches(account)
    return account


async def _prefetch_caches(account: dict) -> None:
    """登录归档后 best-effort 预拉签到状态与配额（弹窗承诺「授权完成后自动刷新
    资源包配额数据」；不做的话已签到账号首屏会误显「今日未签」，需手动刷新状态）。

    预取失败只告警不回滚登录：账号已入库，卡片上的「刷新状态/刷新配额」可手动补。
    """
    from relay.platforms.workbuddy import billing
    try:
        await billing.refresh_checkin_status(account)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[workbuddy] 预拉签到状态失败 uid=%s: %s", account.get("uid"), exc)
    try:
        await billing.fetch_quota(account)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[workbuddy] 预拉配额失败 uid=%s: %s", account.get("uid"), exc)


async def login_status(login_id: str) -> dict:
    """查询登录进度；完成/过期的槽查询后弹出（完成即弹出，防 task 泄漏）。"""
    slot = _slots.get(login_id)
    if slot is None:
        return {"status": "failed", "error": "登录请求不存在或已过期，请重新发起"}
    if slot.task is not None and slot.task.done() and slot.status == "pending":
        # task 已结束但未置态（异常路径兜底）
        slot.status = "failed"
        slot.error = slot.error or "登录中断"
    if slot.status == "pending" and time.monotonic() > slot.expires_at:
        slot.status = "failed"
        slot.error = "登录超时，请重试"
    result: dict = {"status": slot.status}
    if slot.status == "success":
        result["uid"] = slot.account_uid
    if slot.status == "failed":
        result["error"] = slot.error or "登录失败"
    if slot.status != "pending":
        _slots.pop(login_id, None)
    else:
        _cleanup_expired_slots()
    return result


def _cleanup_expired_slots() -> None:
    for lid, s in list(_slots.items()):
        if s.status != "pending" or time.monotonic() > s.expires_at:
            if s.task is not None and not s.task.done():
                s.task.cancel()
            _slots.pop(lid, None)


async def cancel_login(login_id: str) -> None:
    """取消轮询 task 并移除槽。"""
    slot = _slots.pop(login_id, None)
    if slot is not None and slot.task is not None and not slot.task.done():
        slot.task.cancel()


async def refresh_token(account: dict) -> dict:
    """刷新 token 并回写存储；拒绝（401/业务失败）→ login_required 引导重登。"""
    refresh_tok = account.get("refresh_token") or ""
    if not refresh_tok:
        raise PlatformAuthError("缺少 refresh_token，请重新登录")
    url = f"{settings.wb_web_base}{WB_API_PREFIX}/auth/token/refresh"
    headers = refresh_headers(access_token=account.get("access_token") or "",
                              refresh_token=refresh_tok,
                              domain=account.get("domain") or "")
    try:
        body = await _client.request_json("POST", url, headers=headers, json_body={})
    except PlatformAuthError:
        raise
    except PlatformUpstreamError as exc:
        kind = "login_required" if exc.status in (401, 403) else "refresh_failed"
        raise PlatformAuthError(f"刷新 token 失败: {exc}", kind=kind) from exc
    data = parse_envelope(body, context="refresh")
    new_access = str(data.get("accessToken") or data.get("access_token") or "")
    if not new_access:
        raise PlatformAuthError("刷新响应缺少 accessToken", kind="refresh_failed")
    patch: dict = {"access_token": new_access, "expires_at": _expiry_ms(data)}
    new_refresh = data.get("refreshToken") or data.get("refresh_token")
    if new_refresh:
        patch["refresh_token"] = str(new_refresh)
    return await store.update_account(PLATFORM_ID, account["uid"], patch)
