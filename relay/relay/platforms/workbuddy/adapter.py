"""WorkBuddy 平台适配器：编排 oauth / billing / store，实现 PlatformAdapter 契约。

双域（双平台拆分）：每实例持一个 `WbRealm` profile；CN 默认（`WorkBuddyAdapter()`
与 `WorkBuddyAdapter(REALM_CN)` 等价，零回归）。账号池按 `profile.platform_id`
自动隔离（store 按 platform_id 分文件）。
"""
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
from relay.platforms.workbuddy.realms import REALM_CN, WbRealm

# 兼容常量（存量测试/脚本引用）：CN platform_id 直通 profile 取值
PLATFORM_ID = REALM_CN.platform_id

# per-uid 刷新锁（防 stampede：并发请求只触发一次 refresh，对齐 ta3 _refresh_locks）
_refresh_locks: dict[str, asyncio.Lock] = {}


class WorkBuddyAdapter:
    """账号型平台：多账号登录（设备授权）/ 手动签到 / 配额查询。

    profile：域 profile（默认 CN——现网形态零回归）。
    """

    def __init__(self, profile: WbRealm = REALM_CN) -> None:
        self.profile = profile

    @property
    def platform_id(self) -> str:
        return self.profile.platform_id

    def info(self) -> PlatformInfo:
        return PlatformInfo(
            id=self.profile.platform_id,
            display_name=self.profile.display_name,
            supports_checkin=self.profile.supports_checkin,
            supports_quota=self.profile.supports_quota,
        )

    # ─────────────────────── token 生命周期 ───────────────────────

    async def ensure_token(self, account: dict) -> dict:
        """token 有效则原样返回；将过期时 per-uid Lock 双检刷新（先查库再刷，防并发重刷）。"""
        uid = str(account.get("uid") or "")
        if not oauth._will_expire_soon(account):
            return account
        lock = _refresh_locks.setdefault(uid, asyncio.Lock())
        async with lock:
            fresh = await store.find_account(self.platform_id, uid)
            current = fresh if fresh is not None else account
            if not oauth._will_expire_soon(current):
                return current
            return await oauth.refresh_token(current, profile=self.profile)

    def _require_account(self, account: dict | None) -> dict:
        if account is None:
            raise PlatformAuthError("账号不存在，请重新登录")
        return account

    async def _account_with_valid_token(self, uid: str) -> dict:
        """取账号并确保 token 可用（统一入口：查库 → ensure_token）。"""
        account = self._require_account(await store.find_account(self.platform_id, uid))
        return await self.ensure_token(account)

    # ─────────────────────── 账号管理 ───────────────────────

    async def accounts(self) -> list[AccountView]:
        return [AccountView.from_dict(a)
                for a in await store.load_accounts(self.platform_id)]

    async def delete_account(self, uid: str) -> bool:
        return await store.delete_account(self.platform_id, uid)

    # ─────────────────────── 登录（委托 oauth）───────────────────────

    async def start_login(self) -> dict:
        return await oauth.start_login(profile=self.profile)

    async def login_status(self, login_id: str) -> dict:
        return await oauth.login_status(login_id)

    async def cancel_login(self, login_id: str) -> None:
        await oauth.cancel_login(login_id)

    # ─────────────────────── 签到与配额（委托 billing）───────────────────────

    async def checkin(self, uid: str) -> tuple[CheckinResult, AccountView]:
        account = await self._account_with_valid_token(uid)
        result, updated = await billing.perform_checkin(account, profile=self.profile)
        view = await self._view_of(updated)
        return result, view

    async def refresh_checkin_status(self, uid: str) -> AccountView:
        account = await self._account_with_valid_token(uid)
        if not self.profile.supports_checkin:
            # 国际版无签到体系（定稿）：零上游调用直接返回当前账号视图
            return await self._view_of(account)
        updated = await billing.refresh_checkin_status(account, profile=self.profile)
        return await self._view_of(updated)

    async def refresh_quota(self, uid: str) -> AccountView:
        account = await self._account_with_valid_token(uid)
        updated = await billing.fetch_quota(account, profile=self.profile)
        return await self._view_of(updated)

    async def _view_of(self, account: dict) -> AccountView:
        return AccountView.from_dict(account)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _quota_payload(snapshot: QuotaSnapshot) -> dict:
    return {"total": snapshot.total, "remaining": snapshot.remaining,
            "used": snapshot.used, "expire_at": snapshot.expire_at,
            "fetched_at": snapshot.fetched_at}
