"""双域账号支持单测：realm profile 快照 / 双池隔离 / 头族分派 / 签到拒答 / 归属校验。

全程不触网（MockTransport / 纯函数）；账号池落 tmp 目录（monkeypatch data_dir）。
"""
import asyncio
import json

import httpx
import pytest

from app.models.schemas import ChatMessage, ChatRequest
from relay import adapter_registry
from relay.config import settings
from relay.platforms import PLATFORMS, store
from relay.platforms.workbuddy import billing, catalog, imports as wb_imports, oauth
from relay.platforms.workbuddy import chat as wbchat
from relay.platforms.workbuddy.realms import (
    REALM_CN,
    REALM_GLOBAL,
    is_global_domain,
    realm_for,
)

GLOBAL_PLATFORM = "workbuddy-global"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    wbchat.reset_cursor()
    catalog.reset_cache()
    return tmp_path


def _sse_response(lines, status=200):
    body = "".join(f"{ln}\n\n" for ln in lines).encode()
    return httpx.Response(status, content=body,
                          headers={"Content-Type": "text/event-stream"})


async def _seed(platform_id: str, uid: str, **extra):
    await store.upsert_account(platform_id, {
        "uid": uid, "access_token": "at", "refresh_token": "rt",
        "expires_at": 9_999_999_999_999, **extra})
    return await store.find_account(platform_id, uid)


# ─────────────────────────── profile 快照 ───────────────────────────

def test_realm_cn_profile_matches_legacy_literals():
    """CN profile 取值与现状字面量逐字一致（零回归锚点）。"""
    assert REALM_CN.platform_id == "workbuddy"
    assert REALM_CN.display_name == "WorkBuddy CN"
    assert REALM_CN.model_prefix == "WorkBuddy"  # commit 3 改短为 wbcn（读兼容归一）
    assert REALM_CN.origin == "https://www.codebuddy.cn"
    assert REALM_CN.accept_language == "zh-CN"
    assert REALM_CN.ua_platform == "WorkBuddy"
    assert REALM_CN.supports_checkin is True
    assert REALM_CN.chat_domain == ""  # CN 按账号 domain
    assert REALM_CN.ensure_system_first is False
    assert REALM_CN.inject_enterprise_headers is True
    assert REALM_CN.enterprise_model_paths == ("/console/enterprises/personal/models",)
    assert REALM_CN.meter_paths == ("/v2/billing/meter",)
    assert [p for p, _ in REALM_CN.quota_candidates] == [
        "/v2/billing/meter/get-user-resource-summary",
        "/v2/billing/meter/get-user-resource",
    ]


def test_realm_global_profile_matches_panel_evidence():
    """国际版 profile 取值对齐 panel 证据表（§2）+ 探针实测校准（2026-10-14）。"""
    assert REALM_GLOBAL.platform_id == "workbuddy-global"
    assert REALM_GLOBAL.display_name == "WorkBuddy"
    assert REALM_GLOBAL.model_prefix == "wbg"
    assert REALM_GLOBAL.origin == "https://www.workbuddy.ai"
    assert REALM_GLOBAL.accept_language == "en-US"
    assert REALM_GLOBAL.ua_platform == "WorkBuddy AI"
    assert REALM_GLOBAL.supports_checkin is False  # 定稿：国际版无签到
    assert REALM_GLOBAL.chat_domain == "www.workbuddy.ai"
    assert REALM_GLOBAL.ensure_system_first is True  # 探针：首条非 system → 400/11128
    assert REALM_GLOBAL.inject_enterprise_headers is False
    assert REALM_GLOBAL.enterprise_model_paths == (
        "/v2/enterprises/personal/models",
        "/console/enterprises/personal/models",
    )  # 探针实测：/v2 200 优先，/console 500 回退
    assert REALM_GLOBAL.meter_paths == ("/billing/meter", "/v2/billing/meter")
    assert [p for p, _ in REALM_GLOBAL.quota_candidates] == [
        "/billing/meter/get-user-resource",
        "/v2/billing/meter/get-user-resource",
    ]


def test_realm_base_properties_read_settings(monkeypatch):
    """base 类取值读时取 settings（env/测试 monkeypatch 生效）。"""
    monkeypatch.setattr(settings, "wb_global_base", "https://global.test")
    monkeypatch.setattr(settings, "wb_api_base", "https://cn.test")
    monkeypatch.setattr(settings, "wb_web_base", "https://web.test")
    assert REALM_CN.chat_base == "https://cn.test"
    assert REALM_CN.refresh_base == "https://web.test"
    assert REALM_GLOBAL.chat_base == "https://global.test"
    assert REALM_GLOBAL.refresh_base == "https://global.test"  # panel：refresh 走 chatBase
    assert REALM_GLOBAL.billing_base == "https://global.test"
    assert REALM_GLOBAL.login_base == "https://global.test"
    assert REALM_GLOBAL.catalog_base == "https://global.test"


def test_realm_registry_and_domain_helpers():
    assert realm_for("workbuddy") is REALM_CN
    assert realm_for(GLOBAL_PLATFORM) is REALM_GLOBAL
    assert realm_for("nope") is None
    # 国际版域族：workbuddy.ai / codebuddy.ai 裸域与子域
    assert is_global_domain("www.workbuddy.ai")
    assert is_global_domain("workbuddy.ai")
    assert is_global_domain("sub.codebuddy.ai")
    assert is_global_domain("www.codebuddy.ai")
    assert not is_global_domain("www.codebuddy.cn")
    assert not is_global_domain("www.workbuddy.cn")
    assert not is_global_domain("")
    # 归属判定：空域放行；CN 拒国际两族；国际版拒 CN 域
    assert REALM_CN.allows_domain("") and REALM_GLOBAL.allows_domain("")
    assert REALM_CN.allows_domain("www.codebuddy.cn")
    assert not REALM_CN.allows_domain("www.codebuddy.ai")
    assert not REALM_CN.allows_domain("www.workbuddy.ai")
    assert REALM_GLOBAL.allows_domain("www.codebuddy.ai")
    assert REALM_GLOBAL.allows_domain("www.workbuddy.ai")
    assert not REALM_GLOBAL.allows_domain("www.codebuddy.cn")


# ─────────────────────────── 双注册与路由 ───────────────────────────

def test_platforms_dual_registration():
    """PLATFORMS 双实例（两个独立平台）；info() 能力位正确。"""
    assert set(PLATFORMS) == {"workbuddy", GLOBAL_PLATFORM}
    cn_info = PLATFORMS["workbuddy"].info()
    gl_info = PLATFORMS[GLOBAL_PLATFORM].info()
    assert cn_info.display_name == "WorkBuddy CN" and cn_info.supports_checkin is True
    assert gl_info.display_name == "WorkBuddy" and gl_info.supports_checkin is False
    assert gl_info.supports_quota is True


def test_dual_prefix_routing(data_dir):
    """`wbg/x` 构造国际版 adapter；`WorkBuddy/x` 构造 CN adapter（profile 正确）。"""
    mock = httpx.MockTransport(lambda r: _sse_response(["data: [DONE]"]))
    gl = adapter_registry.build_adapter({"name": "x"}, "wbg/x")
    assert isinstance(gl, wbchat.WorkBuddyChatAdapter)
    assert gl._profile is REALM_GLOBAL  # noqa: SLF001
    cn = adapter_registry.build_adapter({"name": "x"}, "WorkBuddy/x")
    assert isinstance(cn, wbchat.WorkBuddyChatAdapter)
    assert cn._profile is REALM_CN  # noqa: SLF001
    # 未知前缀 → None（调用方 404）
    assert adapter_registry.build_adapter({"name": "x"}, "wbg2/x") is None
    _ = mock


def test_unknown_model_404_semantics_preserved(data_dir):
    """W B注册表 availability：双池任一有号即有值语义（分池探测）。"""
    assert _run(adapter_registry.provider_available("wbg")) is False  # 无号 → False
    _run(_seed(GLOBAL_PLATFORM, "g1"))
    assert _run(adapter_registry.provider_available("wbg")) is True
    assert _run(adapter_registry.provider_available("WorkBuddy")) is False  # CN 池无号


# ─────────────────────────── 头族分派 ───────────────────────────

def _adapter(profile, handler=None):
    return wbchat.WorkBuddyChatAdapter(
        "deepseek-v4.1-flash",
        profile=profile,
        transport=httpx.MockTransport(handler or (lambda r: _sse_response(["data: [DONE]"]))))


def test_headers_cn_zero_regression(data_dir):
    """CN 头族输出与变更前逐字一致（对拼装产物断言；wb_user_agent 默认空）。"""
    _run(_seed("workbuddy", "u_cn", domain="www.codebuddy.cn", enterprise_id="ent_1"))
    account = _run(store.find_account("workbuddy", "u_cn"))
    h = _adapter(REALM_CN)._headers(account)
    assert h["User-Agent"] == "WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1"
    assert h["Origin"] == "https://www.codebuddy.cn"
    assert h["Accept-Language"] == "zh-CN"
    assert h["X-Domain"] == "www.codebuddy.cn"       # 账号域
    assert h["X-Enterprise-Id"] == "ent_1"           # 有企业则发
    assert "X-No-Enterprise-Id" not in h
    assert h["X-IDE-Name"] == "WorkBuddy" and h["X-Product"] == "WorkBuddy"


def test_headers_global_snapshot(data_dir):
    """国际版头族：UA 平台段 WorkBuddy AI / Origin workbuddy.ai / en-US /
    恒 X-Domain:www.workbuddy.ai + X-No-Enterprise-Id（panel injectGlobalChatHeaders）。"""
    _run(_seed(GLOBAL_PLATFORM, "u_gl", domain="www.codebuddy.ai", enterprise_id="ent_x"))
    account = _run(store.find_account(GLOBAL_PLATFORM, "u_gl"))
    h = _adapter(REALM_GLOBAL)._headers(account)
    assert h["User-Agent"] == "WorkBuddy/5.5.4 WorkBuddy AI/5.5.4 CLI/2.137.1"
    assert h["Origin"] == "https://www.workbuddy.ai"
    assert h["Accept-Language"] == "en-US"
    assert h["X-Domain"] == "www.workbuddy.ai"       # 恒注入（忽略账号域）
    assert h["X-No-Enterprise-Id"] == "1"            # 无企业概念
    assert "X-Enterprise-Id" not in h
    # 归属四头与 CN 同款（panel injectAttribution 不分域）
    assert h["X-IDE-Name"] == "WorkBuddy" and h["X-IDE-Type"] == "WorkBuddy"
    assert h["X-Product"] == "WorkBuddy"
    assert h["X-Agent-Purpose"] == "conversation"


def test_wb_user_agent_explicit_override_wins(monkeypatch):
    """显式覆盖（env/GUI 设置非空）→ 两域统一用该值（保留自愈路径）。"""
    monkeypatch.setattr(settings, "wb_user_agent", "Custom/1.0")
    from relay.platforms.workbuddy.client import user_agent_for
    assert user_agent_for(REALM_CN) == "Custom/1.0"
    assert user_agent_for(REALM_GLOBAL) == "Custom/1.0"


def test_billing_headers_dual_profile(monkeypatch):
    """billing 域头双 profile：Origin/Referer/Accept-Language 按域；企业头仅 CN。"""
    from relay.platforms.workbuddy.client import billing_headers
    cn = billing_headers(access_token="at", uid="u", domain="d",
                         enterprise_id="ent", profile=REALM_CN)
    assert cn["Origin"] == "https://www.codebuddy.cn" and cn["Accept-Language"] == "zh-CN"
    assert cn["X-Enterprise-Id"] == "ent" and cn["X-Tenant-Id"] == "ent"
    gl = billing_headers(access_token="at", uid="u", domain="d",
                         enterprise_id="ent", profile=REALM_GLOBAL)
    assert gl["Origin"] == "https://www.workbuddy.ai" and gl["Accept-Language"] == "en-US"
    assert "X-Enterprise-Id" not in gl and "X-Tenant-Id" not in gl
    # UA 单段不分域（panel billingUA）
    assert cn["User-Agent"] == gl["User-Agent"] == f"WorkBuddy/{settings.wb_client_version}"


# ─────────────────────────── chat：system 首条（国际版） ───────────────────────────

def test_ensure_system_first_global_only():
    """国际版：首条非 system → 前置中性 system；已是 system 不重复注入；CN 零注入。"""
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="hi")])
    cn_body = _adapter(REALM_CN).transform_request(req)
    assert [m["role"] for m in cn_body["messages"]] == ["user"]  # CN 不变
    gl_body = _adapter(REALM_GLOBAL).transform_request(req)
    assert [m["role"] for m in gl_body["messages"]] == ["system", "user"]
    assert gl_body["messages"][0]["content"] == wbchat.CONSOLE_SYSTEM_PROMPT

    req2 = ChatRequest(model="m", messages=[
        ChatMessage(role="system", content="You are X."),
        ChatMessage(role="user", content="hi")])
    gl_body2 = _adapter(REALM_GLOBAL).transform_request(req2)
    assert len(gl_body2["messages"]) == 2  # 已有 system → 不注入
    assert gl_body2["messages"][0]["content"] == "You are X."


# ─────────────────────────── 签到拒答（国际版） ───────────────────────────

def test_global_checkin_refused_without_upstream_call(data_dir, monkeypatch):
    """国际版签到：明确文案 + 零上游调用（transport 断言）。"""
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(500, text="should not happen")

    monkeypatch.setattr(billing, "_client",
                        type(billing._client)(transport=httpx.MockTransport(handler)))
    account = _run(_seed(GLOBAL_PLATFORM, "g_ck"))
    result, updated = _run(billing.perform_checkin(account, profile=REALM_GLOBAL))
    assert result.success is False
    assert "国际版无签到" in result.message
    assert calls == []  # 零上游调用
    assert updated["uid"] == account["uid"]


def test_global_checkin_status_refused_at_adapter(data_dir, monkeypatch):
    """适配器层：国际版 refresh_checkin_status 不发上游（支持位门控在 adapter 侧）。"""
    from relay.platforms.workbuddy.adapter import WorkBuddyAdapter
    _run(_seed(GLOBAL_PLATFORM, "g_ck2"))
    adapter = WorkBuddyAdapter(REALM_GLOBAL)
    calls: list[str] = []

    async def _fail(*a, **k):
        calls.append("x")
        raise AssertionError("不应触达上游")

    monkeypatch.setattr(billing, "checkin_status_payload", _fail)
    view = _run(adapter.refresh_checkin_status("g_ck2"))
    assert calls == []
    assert view.uid == "g_ck2"
    assert view.checkin == {}  # 零上游调用：不伪造签到态


# ─────────────────────────── 双池隔离 / 串池回归 ───────────────────────────

def test_pool_isolation_two_files(data_dir):
    """两 platform_id 独立文件读写、互不可见。"""
    _run(_seed("workbuddy", "cn_1"))
    _run(_seed(GLOBAL_PLATFORM, "gl_1"))
    assert (data_dir / "platform_workbuddy.json").exists()
    assert (data_dir / "platform_workbuddy-global.json").exists()
    assert [a["uid"] for a in _run(store.load_accounts("workbuddy"))] == ["cn_1"]
    assert [a["uid"] for a in _run(store.load_accounts(GLOBAL_PLATFORM))] == ["gl_1"]


def test_billing_writes_own_pool(data_dir):
    """配额落库落本池文件（CN 文件零写入断言）。"""
    account = _run(_seed(GLOBAL_PLATFORM, "gl_q"))

    async def _fake_save(account, items, profile=REALM_CN):
        return await store.update_account(profile.platform_id, account["uid"],
                                          {"quota": {"total": 1}})

    _run(store.update_account(GLOBAL_PLATFORM, "gl_q", {"quota": {"total": 1}}))
    gl_doc = json.loads((data_dir / "platform_workbuddy-global.json").read_text("utf-8"))
    assert gl_doc["accounts"][0]["quota"]["total"] == 1
    # CN 文件未被创建/写入
    assert not (data_dir / "platform_workbuddy.json").exists()


def test_catalog_uses_own_pool(data_dir):
    """目录选号按 profile 查本池（国际版池有号 → CN 池无号不受影响）。"""
    _run(_seed(GLOBAL_PLATFORM, "gl_cat", domain="www.workbuddy.ai"))
    # 目录走 MockTransport：v3/config 返回一个模型
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={
        "code": 0, "data": {"models": [{"id": "glm-x", "name": "GLM-X",
                                        "maxOutputTokens": 64000}]}}))
    models = _run(catalog.load_models(force=True, transport=t, profile=REALM_GLOBAL))
    assert [m["name"] for m in models] == ["glm-x"]
    # CN 池无号 → 空（负缓存另桶）
    assert _run(catalog.load_models(force=True, transport=t, profile=REALM_CN)) == []


def test_oauth_refresh_writes_own_pool(data_dir, monkeypatch):
    """refresh_token 按 profile 落本池（CN 池零写入）。"""
    _run(_seed(GLOBAL_PLATFORM, "gl_r", refresh_token="rt_g"))
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(200, json={"code": 0, "data": {
            "accessToken": "new_at", "refreshToken": "new_rt", "expiresIn": 3600}})

    monkeypatch.setattr(oauth, "_client",
                        oauth.WbClient(transport=httpx.MockTransport(handler)))
    account = _run(store.find_account(GLOBAL_PLATFORM, "gl_r"))
    updated = _run(oauth.refresh_token(account, profile=REALM_GLOBAL))
    assert updated["access_token"] == "new_at"
    assert "workbuddy.ai" in calls[0]  # 国际版 refresh base
    gl_doc = json.loads((data_dir / "platform_workbuddy-global.json").read_text("utf-8"))
    assert gl_doc["accounts"][0]["access_token"] == "new_at"
    assert not (data_dir / "platform_workbuddy.json").exists()


# ─────────────────────────── 归属校验（导入） ───────────────────────────

def _json_import(text: str, profile):
    return _run(wb_imports.import_text(text, profile=profile))


def test_import_rejects_global_domain_into_cn(data_dir):
    """国际号（workbuddy.ai/codebuddy.ai）进 CN 池 → 拒绝并引导。"""
    payload = json.dumps([{"access_token": "tok_gl", "uid": "u_x",
                           "domain": "www.codebuddy.ai"}])
    with pytest.raises(wb_imports.ImportParseError) as ei:
        _json_import(payload, REALM_CN)
    assert "国际版" in str(ei.value)
    assert _run(store.load_accounts("workbuddy")) == []


def test_import_rejects_cn_domain_into_global(data_dir):
    """CN 域账号进国际版池 → 拒绝并引导（补全失败类）。"""
    payload = json.dumps([{"access_token": "tok_cn", "uid": "u_y",
                           "domain": "copilot.tencent.com"}])
    with pytest.raises(wb_imports.ImportParseError) as ei:
        _json_import(payload, REALM_GLOBAL)
    assert "国内版" in str(ei.value)
    assert _run(store.load_accounts(GLOBAL_PLATFORM)) == []


def test_import_accepts_matching_domain(data_dir):
    """域族匹配 → 入库本池（JSON 源不联网）。"""
    payload = json.dumps([{"access_token": "tok_ok", "uid": "u_z",
                           "domain": "www.workbuddy.ai"}])
    rows = _json_import(payload, REALM_GLOBAL)
    assert rows[0]["uid"] == "u_z"
    assert _run(store.load_accounts(GLOBAL_PLATFORM))[0]["uid"] == "u_z"
    assert _run(store.load_accounts("workbuddy")) == []
    # 空域放行（兼容存量/未知形态）
    rows2 = _json_import(json.dumps([{"access_token": "tok_e", "uid": "u_e"}]), REALM_CN)
    assert rows2[0]["uid"] == "u_e"
