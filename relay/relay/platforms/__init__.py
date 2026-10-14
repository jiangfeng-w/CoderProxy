"""平台注册表：账号型平台适配器在此登记，routes 按 /v1/platforms/{platform} 路径参数分发。

Trae/Qoder 后续平行加入：实现 PlatformAdapter 后在此注册即可（主干零改动）。

双域（2026-10-14）：WorkBuddy CN 与 WorkBuddy 国际版是**两个独立平台实例**
（各自账号池 / 模型前缀 / tab；realm 差异收敛在 profile，见 realms.py）。
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
from relay.platforms.workbuddy.realms import REALM_CN, REALM_GLOBAL

PLATFORMS: dict[str, PlatformAdapter] = {
    REALM_CN.platform_id: WorkBuddyAdapter(REALM_CN),
    REALM_GLOBAL.platform_id: WorkBuddyAdapter(REALM_GLOBAL),
}


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
