"""平台插件底座：账号型平台（WorkBuddy，后续 Trae/Qoder 平行加入）的抽象与数据契约。

- PlatformAdapter：typing.Protocol（非 ABC）——只约束账号型平台，ta3（vendored 牛码）
  不纳入本体系；新平台实现 Protocol 后在 platforms/__init__.py 登记即可。
- 错误映射（routes 层执行）：PlatformAuthError(kind=login_required) → 401、
  PlatformUpstreamError → 502、httpx.HTTPError → 504。
- AccountView：对外账号视图，**不含任何 token**——routes 直接序列化返回前端。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class PlatformError(Exception):
    """平台插件错误基类（routes 按子类映射 HTTP 状态码）。"""


class PlatformAuthError(PlatformError):
    """登录态错误。kind: login_required（前端引导重登）/ refresh_failed / network。"""

    def __init__(self, message: str, kind: str = "login_required"):
        super().__init__(message)
        self.kind = kind


class PlatformUpstreamError(PlatformError):
    """上游业务错误（{code,msg} 信封非成功码 / 上游 HTTP 4xx、5xx）。

    status 供 routes 透传真实上游状态码（缺省 502）。
    """

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass
class PlatformInfo:
    """平台元信息（GET /v1/platforms 列表项）。"""

    id: str
    display_name: str
    supports_checkin: bool = False
    supports_quota: bool = False


@dataclass
class AccountView:
    """对外账号视图（不含任何 token）。"""

    uid: str
    nickname: str = ""
    email: str = ""
    domain: str = ""
    status: str = "normal"
    checkin: dict = field(default_factory=dict)
    quota: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, account: dict) -> AccountView:
        """由存储行构造视图；敏感字段（access/refresh token）在此天然不透出。"""
        return cls(
            uid=str(account.get("uid") or ""),
            nickname=str(account.get("nickname") or ""),
            email=str(account.get("email") or ""),
            domain=str(account.get("domain") or ""),
            status=str(account.get("status") or "normal"),
            checkin=dict(account.get("checkin") or {}),
            quota=dict(account.get("quota") or {}),
        )

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "nickname": self.nickname,
            "email": self.email,
            "domain": self.domain,
            "status": self.status,
            "checkin": self.checkin,
            "quota": self.quota,
        }


@dataclass
class CheckinResult:
    """手动签到结果（幂等：已签到按成功返回并注明）。"""

    success: bool
    message: str = ""
    streak_days: int = 0
    credit: int | None = None
    today_checked_in: bool = True


@dataclass
class QuotaSnapshot:
    """配额快照（多形态响应聚合后的归一结果）。"""

    total: float = 0.0
    remaining: float = 0.0
    used: float = 0.0
    expire_at: str = ""
    fetched_at: str = ""


@runtime_checkable
class PlatformAdapter(Protocol):
    """账号型平台适配器契约。不支持的能力（如无签到）抛 PlatformError。"""

    def info(self) -> PlatformInfo: ...

    async def accounts(self) -> list[AccountView]: ...

    async def start_login(self) -> dict:
        """发起设备授权登录：返回 {login_id, authorize_url, expires_in}。"""

    async def login_status(self, login_id: str) -> dict:
        """查询登录进度：{status: pending|success|failed, error?}；成功后账号入库。"""

    async def cancel_login(self, login_id: str) -> None: ...

    async def delete_account(self, uid: str) -> bool: ...

    async def checkin(self, uid: str) -> tuple[CheckinResult, AccountView]: ...

    async def refresh_checkin_status(self, uid: str) -> AccountView: ...

    async def refresh_quota(self, uid: str) -> AccountView: ...


def view_payload(view: AccountView) -> dict[str, Any]:
    """AccountView → JSON 载荷（routes 统一出口）。"""
    return view.to_dict()
