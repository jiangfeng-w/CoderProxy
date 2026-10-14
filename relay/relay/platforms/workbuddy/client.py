"""WorkBuddy HTTP 客户端：三域指纹头组装 + {code,msg,data} 信封解析 + 直连。

指纹三域（WorkBuddy多账号登录与签到 spec「API 契约」节，三方参考交叉验证）：
- plugin 域（auth/state、auth/token、login/account）：X-No-* 无值声明头族；
  login/account 起带 Bearer 与 X-Domain
- billing 域（签到/配额）：单段 UA `WorkBuddy/<ver>`、Origin/Referer www.codebuddy.cn、
  X-CodeBuddy-Request: 1、Accept-Language zh-CN、Authorization、X-User-Id、X-Domain、
  X-Machine-ID / X-Session-ID（sha256("wbcp:{purpose}:{uid}") 截 36 hex 稳定派生：
  同号恒同值防指纹漂移、异号互异防多号设备关联）
- refresh 域：X-Refresh-Token 头**只允许出现在 refresh 请求**

直连纪律（M1 结论）：所有 httpx.AsyncClient 必须 trust_env=False，绕系统代理；
测试经 WbClient(transport=...) 注入 httpx.MockTransport，不触网。
"""
from __future__ import annotations

import hashlib

import httpx

from relay.config import settings
from relay.platforms.base import PlatformAuthError, PlatformUpstreamError
from relay.platforms.workbuddy.realms import REALM_CN, WbRealm

WB_API_PREFIX = "/v2/plugin"

# {code,...} 信封成功码：官方客户端语义 code===0，部分端点（auth/token、refresh）
# 返回 200 —— 参考实现 code==0 || code==200 双收；签到等业务端点由调用方收窄
DEFAULT_OK_CODES = (0, 200)

CN_ORIGIN = "https://www.codebuddy.cn"


def user_agent_for(profile: WbRealm = REALM_CN) -> str:
    """出站 UA：显式覆盖优先（settings.wb_user_agent 非空，两域统一用该值——
    保留用户自愈路径）；否则按 profile 拼三段式（CN 平台段 `WorkBuddy` /
    国际版 `WorkBuddy AI`，panel defaultWorkBuddyUAFor）。"""
    if settings.wb_user_agent:
        return settings.wb_user_agent
    v = settings.wb_client_version
    return (f"WorkBuddy/{v} {profile.ua_platform}/{v} "
            f"CLI/{settings.wb_cli_version}")


def origin_for(profile: WbRealm = REALM_CN) -> str:
    """域名来源（Origin/Referer 根）：CN codebuddy.cn / 国际版 workbuddy.ai。"""
    return profile.origin


def accept_language_for(profile: WbRealm = REALM_CN) -> str:
    """Accept-Language 按域切（panel acceptLanguageFor）：CN zh-CN / 国际版 en-US。"""
    return profile.accept_language


def derive_stable_id(uid: str, purpose: str) -> str:
    """账号级稳定派生 36 hex：跨重启恒定、同号同用途同值、异号互异。"""
    digest = hashlib.sha256(f"wbcp:{purpose}:{uid}".encode()).hexdigest()
    return digest[:36]


def plugin_headers(*, access_token: str = "", domain: str = "") -> dict[str, str]:
    """plugin 域头：X-No-* 声明「本请求无这些标识」；带 token 后加 Bearer 与 X-Domain。

    双域同构（plugin 幂等声明族两域都接受——2026-10-14 国际版小号探针实测
    auth/state 与 plugin/accounts 均 200）。
    """
    headers = {
        "X-No-Authorization": "true",
        "X-No-User-Id": "true",
        "X-No-Enterprise-Id": "true",
        "X-No-Department-Info": "true",
    }
    if access_token:
        headers["Authorization"] = f"Bearer {access_token}"
        # 已带鉴权后 X-No-Authorization 语义失效，login/account 实测形态为去除该声明
        headers.pop("X-No-Authorization", None)
    if domain:
        headers["X-Domain"] = domain
    return headers


def billing_headers(*, access_token: str, uid: str, domain: str = "",
                    enterprise_id: str = "",
                    profile: WbRealm = REALM_CN) -> dict[str, str]:
    """billing 域头：白名单类接口官方 UA 为单段 WorkBuddy/<ver>（无 CLI 段，两域同款）。

    双域差异（spec §2 表）：Origin/Referer、Accept-Language 按 profile；
    X-Enterprise-Id / X-Tenant-Id 仅 CN 注入（国际版无企业概念）。
    """
    origin = origin_for(profile)
    headers = {
        "User-Agent": f"WorkBuddy/{settings.wb_client_version}",
        "Origin": origin,
        "Referer": f"{origin}/",
        "X-CodeBuddy-Request": "1",
        "Accept-Language": accept_language_for(profile),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }
    if uid:
        headers["X-User-Id"] = uid
        headers["X-Machine-ID"] = derive_stable_id(uid, "machine")
        headers["X-Session-ID"] = derive_stable_id(uid, "session")
    if domain:
        headers["X-Domain"] = domain
    if enterprise_id and profile.inject_enterprise_headers:
        headers["X-Enterprise-Id"] = enterprise_id
        headers["X-Tenant-Id"] = enterprise_id
    return headers


def refresh_headers(*, access_token: str, refresh_token: str, domain: str = "",
                    profile: WbRealm = REALM_CN) -> dict[str, str]:
    """refresh 域头：X-Refresh-Token 仅此处出现（spec 契约硬约束）。

    UA 按 profile（国际版平台段 WorkBuddy AI；显式覆盖优先）。
    """
    headers = {
        "User-Agent": user_agent_for(profile),
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "X-Refresh-Token": refresh_token,
        "X-Auth-Refresh-Source": "ide-main",
    }
    if domain:
        headers["X-Domain"] = domain
    return headers


def parse_envelope(body: dict, *, ok_codes: tuple[int, ...] = DEFAULT_OK_CODES,
                   context: str = "") -> dict:
    """{code, msg|message, data} 信封解析：成功返回 data（缺省 {}），业务失败抛错。"""
    code = body.get("code")
    if code is not None and code not in ok_codes:
        msg = body.get("message") or body.get("msg") or "unknown error"
        prefix = f"{context}: " if context else ""
        raise PlatformUpstreamError(f"{prefix}上游业务失败 (code={code}): {msg}")
    data = body.get("data")
    return data if isinstance(data, dict) else ({} if data is None else data)


class WbClient:
    """WorkBuddy 出站 HTTP 客户端。_new_client 为测试注入点（MockTransport）。"""

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        self._transport = transport

    def _new_client(self) -> httpx.AsyncClient:
        # trust_env=False：绕系统代理直连（系统代理劫持 copilot.tencent.com 会 403）
        return httpx.AsyncClient(trust_env=False, timeout=30.0, transport=self._transport)

    async def request_json(self, method: str, url: str, *, headers: dict[str, str],
                           json_body: dict | None = None) -> dict:
        """发请求并解析 JSON 响应体；非 2xx 抛 PlatformUpstreamError，网络错误抛
        PlatformAuthError(kind=network)（routes 统一映射，httpx.HTTPError 不外泄）。"""
        try:
            async with self._new_client() as client:
                resp = await client.request(method, url, headers=headers, json=json_body)
        except httpx.HTTPError as exc:
            raise PlatformAuthError(f"上游网络错误: {exc}", kind="network") from exc
        if resp.status_code >= 400:
            detail = _error_text(resp)
            raise PlatformUpstreamError(
                f"上游 HTTP {resp.status_code}: {detail}", status=resp.status_code)
        try:
            return resp.json()
        except ValueError as exc:
            raise PlatformUpstreamError(f"上游响应非 JSON: {exc}") from exc

    async def close(self) -> None:
        return None


def _error_text(resp: httpx.Response) -> str:
    """尽量从错误响应里抠 message/msg，抠不到退化为截断的原文。"""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict):
        msg = body.get("message") or body.get("msg")
        if msg:
            return str(msg)
    return str(body)[:200]
