"""WorkBuddy HTTP 客户端单测：指纹头三域组装、稳定派生、信封解析、MockTransport 直连。

全程不触网（MockTransport / 纯函数）。
"""
import httpx
import pytest

from relay.config import settings
from relay.platforms.base import PlatformAuthError, PlatformUpstreamError
from relay.platforms.workbuddy.client import (
    WbClient,
    billing_headers,
    derive_stable_id,
    parse_envelope,
    plugin_headers,
    refresh_headers,
)

UID = "u_test"


def test_derive_stable_id_stable_and_distinct():
    """同号同用途恒同值（跨重启稳定）；异号/异用途互异；36 hex 形态。"""
    a1 = derive_stable_id(UID, "machine")
    a2 = derive_stable_id(UID, "machine")
    assert a1 == a2
    assert len(a1) == 36
    int(a1, 16)  # 必须是合法 hex
    assert derive_stable_id(UID, "session") != a1
    assert derive_stable_id("u_other", "machine") != a1


def test_plugin_headers_anonymous_vs_authed():
    """匿名请求带 X-No-* 四声明头；带 token 后 Authorization/X-Domain 且去除 X-No-Authorization。"""
    anon = plugin_headers()
    assert anon["X-No-Authorization"] == "true"
    assert anon["X-No-User-Id"] == "true"
    assert anon["X-No-Enterprise-Id"] == "true"
    assert anon["X-No-Department-Info"] == "true"

    authed = plugin_headers(access_token="at", domain="tencent")
    assert "X-No-Authorization" not in authed
    assert authed["Authorization"] == "Bearer at"
    assert authed["X-Domain"] == "tencent"
    assert authed["X-No-User-Id"] == "true"  # 其余声明头保留


def test_billing_headers_fingerprint():
    """billing 域指纹：单段 UA、Origin/Referer codebuddy.cn、X-CodeBuddy-Request、
    账号级设备头按 uid 稳定派生、企业头双写。"""
    h = billing_headers(access_token="at", uid=UID, domain="tencent",
                        enterprise_id="ent_1")
    assert h["User-Agent"] == f"WorkBuddy/{settings.wb_client_version}"
    assert h["Origin"] == "https://www.codebuddy.cn"
    assert h["Referer"] == "https://www.codebuddy.cn/"
    assert h["X-CodeBuddy-Request"] == "1"
    assert h["Accept-Language"] == "zh-CN"
    assert h["Authorization"] == "Bearer at"
    assert h["X-User-Id"] == UID
    assert h["X-Domain"] == "tencent"
    assert h["X-Machine-ID"] == derive_stable_id(UID, "machine")
    assert h["X-Session-ID"] == derive_stable_id(UID, "session")
    assert h["X-Enterprise-Id"] == "ent_1"
    assert h["X-Tenant-Id"] == "ent_1"

    # 个人号（无 enterprise_id）不带企业头
    h2 = billing_headers(access_token="at", uid=UID)
    assert "X-Enterprise-Id" not in h2 and "X-Tenant-Id" not in h2


def test_refresh_token_header_confined_to_refresh():
    """X-Refresh-Token 只允许出现在 refresh 域头（spec 契约硬约束）。"""
    rh = refresh_headers(access_token="at", refresh_token="rt")
    assert rh["X-Refresh-Token"] == "rt"
    assert rh["X-Auth-Refresh-Source"] == "ide-main"
    assert "X-Refresh-Token" not in billing_headers(access_token="at", uid=UID)
    assert "X-Refresh-Token" not in plugin_headers(access_token="at")


def test_parse_envelope():
    """信封：code 0/200 收；业务码抛 PlatformUpstreamError（msg/message 兜底）；无 data 归 {}。"""
    assert parse_envelope({"code": 0, "data": {"x": 1}}) == {"x": 1}
    assert parse_envelope({"code": 200, "data": {"y": 2}}) == {"y": 2}
    assert parse_envelope({"code": 0}) == {}
    with pytest.raises(PlatformUpstreamError) as ei:
        parse_envelope({"code": 10001, "msg": "已签到", "message": None}, context="checkin")
    assert "10001" in str(ei.value) and "已签到" in str(ei.value)
    with pytest.raises(PlatformUpstreamError):
        parse_envelope({"code": 500, "message": "boom"})


async def test_request_json_via_mock_transport():
    """MockTransport 全链路：头透传、JSON 解析、trust_env=False 直连。"""
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        captured["no_auth"] = request.headers.get("X-No-Authorization")
        return httpx.Response(200, json={"code": 0, "data": {"ok": True}})

    client = WbClient(transport=httpx.MockTransport(handler))
    body = await client.request_json(
        "GET", "https://copilot.tencent.com/v2/plugin/auth/token?state=s1",
        headers=plugin_headers())
    assert body["data"]["ok"] is True
    assert captured["auth"] is None  # 匿名 plugin 请求不带 Bearer
    async with client._new_client() as raw:
        assert raw.trust_env is False


async def test_request_json_maps_http_error():
    """上游 HTTP 500 → PlatformUpstreamError（透传状态码）；连接失败 → PlatformAuthError(network)。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"code": 500, "msg": "server error"})

    client = WbClient(transport=httpx.MockTransport(handler))
    with pytest.raises(PlatformUpstreamError) as ei:
        await client.request_json("POST", "https://copilot.tencent.com/x",
                                  headers=plugin_headers())
    assert ei.value.status == 500
    assert "server error" in str(ei.value)


async def test_request_json_maps_network_error():
    """httpx 网络异常不外泄：包装为 PlatformAuthError(kind=network)（routes 映射 504）。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = WbClient(transport=httpx.MockTransport(handler))
    with pytest.raises(PlatformAuthError) as ei:
        await client.request_json("POST", "https://copilot.tencent.com/x",
                                  headers=plugin_headers())
    assert ei.value.kind == "network"


def test_wb_config_defaults():
    """wb_* 配置项默认值（双口径兜底，小号冒烟后定默认）。"""
    assert settings.wb_api_base.startswith("https://")
    assert settings.wb_web_base == "https://www.codebuddy.cn"
    assert settings.wb_user_agent.startswith("WorkBuddy/")
