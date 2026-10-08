"""WorkBuddy 平台适配器：编排 oauth / billing / store，实现 PlatformAdapter 契约。"""
from __future__ import annotations

import asyncio
import time

from relay.platforms import store
from relay.platforms.base import (
    AccountView,
    CheckinResult,
    PlatformAuthError,
    PlatformInfo,
    QuotaSnapshot,
)
from relay.platforms.workbuddy import billing, oauth

PLATFORM_ID = "workbuddy"

# per-uid 刷新锁（防 stampede：并发请求只触发一次 refresh，对齐 ta3 _refresh_locks）
_refresh_locks: dict[str, asyncio.Lock] = {}


class WorkBuddyAdapter:
    """账号型平台：多账号登录（设备授权）/ 手动签到 / 配额查询。"""

    def info(self) -> PlatformInfo:
        return PlatformInfo(
            id=PLATFORM_ID,
            display_name="WorkBuddy (CodeBuddy CN)",
            supports_checkin=True,
            supports_quota=True,
        )

    # ─────────────────────── token 生命周期 ───────────────────────

    async def ensure_token(self, account: dict) -> dict:
        """token 有效则原样返回；将过期时 per-uid Lock 双检刷新（先查库再刷，防并发重刷）。"""
        uid = str(account.get("uid") or "")
        if not oauth._will_expire_soon(account):
            return account
        lock = _refresh_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            fresh = await store.find_account(PLATFORM_ID, uid)
            current = fresh if fresh is not None else account
            if not oauth._will_expire_soon(current):
                return current
            return await oauth.refresh_token(current)

    def _require_account(self, account: dict | None) -> dict:
        if account is None:
            raise PlatformAuthError("账号不存在，请重新登录")
        return account

    async def _account_with_valid_token(self, uid: str) -> dict:
        """取账号并确保 token 可用（统一入口：查库 → ensure_token）。"""
        account = self._require_account(await store.find_account(PLATFORM_ID, uid))
        return await self.ensure_token(account)

    # ─────────────────────── 账号管理 ───────────────────────

    async def accounts(self) -> list[AccountView]:
        return [AccountView.from_dict(a) for a in await store.load_accounts(PLATFORM_ID)]

    async def delete_account(self, uid: str) -> bool:
        return await store.delete_account(PLATFORM_ID, uid)

    # ─────────────────────── 登录（委托 oauth）───────────────────────

    async def start_login(self) -> dict:
        return await oauth.start_login()

    async def login_status(self, login_id: str) -> dict:
        return await oauth.login_status(login_id)

    async def cancel_login(self, login_id: str) -> None:
        await oauth.cancel_login(login_id)

    # ─────────────────────── 签到与配额（委托 billing，步骤 4）───────────────────────

    async def checkin(self, uid: str) -> tuple[CheckinResult, AccountView]:
        account = await self._account_with_valid_token(uid)
        result, updated = await billing.perform_checkin(account)
        view = await self._view_of(updated)
        return result, view

    async def refresh_checkin_status(self, uid: str) -> AccountView:
        account = await self._account_with_valid_token(uid)
        updated = await billing.refresh_checkin_status(account)
        return await self._view_of(updated)

    async def refresh_quota(self, uid: str) -> AccountView:
        account = await self._account_with_valid_token(uid)
        updated = await billing.fetch_quota(account)
        return await self._view_of(updated)

    async def _view_of(self, account: dict) -> AccountView:
        return AccountView.from_dict(account)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _quota_payload(snapshot: QuotaSnapshot) -> dict:
    return {"total": snapshot.total, "remaining": snapshot.remaining,
            "used": snapshot.used, "expire_at": snapshot.expire_at,
            "fetched_at": snapshot.fetched_at}
