"""平台注册表：账号型平台适配器在此登记，routes 按 /v1/platforms/{platform} 路径参数分发。

Trae/Qoder 后续平行加入：实现 PlatformAdapter 后在此注册即可（主干零改动）。
"""
from __future__ import annotations

from relay.platforms.base import (
    AccountView,
    CheckinResult,
    PlatformAdapter,
    PlatformAuthError,
    PlatformError,
    PlatformInfo,
    PlatformUpstreamError,
    QuotaSnapshot,
)
from relay.platforms.workbuddy.adapter import WorkBuddyAdapter

PLATFORMS: dict[str, PlatformAdapter] = {"workbuddy": WorkBuddyAdapter()}


def get_platform(platform_id: str) -> PlatformAdapter | None:
    return PLATFORMS.get(platform_id)


__all__ = [
    "PLATFORMS",
    "AccountView",
    "CheckinResult",
    "PlatformAdapter",
    "PlatformAuthError",
    "PlatformError",
    "PlatformInfo",
    "PlatformUpstreamError",
    "QuotaSnapshot",
    "get_platform",
]
