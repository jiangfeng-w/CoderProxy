"""WorkBuddy 模型目录单测：双端点并集 / 过滤口径 / 缓存（MockTransport，不触网）。"""
import asyncio
import json

import httpx
import pytest

from relay.config import settings
from relay.platforms import store
from relay.platforms.workbuddy import catalog
from relay.platforms.workbuddy.realms import REALM_GLOBAL

V3_URL = f"{settings.wb_api_base}{catalog.V3_CONFIG_PATH}"
ENT_URL = f"{settings.wb_api_base}{catalog.ENTERPRISE_MODELS_PATH}"


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    catalog.reset_cache()
    return tmp_path


async def _seed_account(uid="u_cat", **extra):
    await store.upsert_account("workbuddy", {
        "uid": uid, "access_token": "at", "refresh_token": "rt",
        "expires_at": 9_999_999_999_999, **extra})
    account = await store.find_account("workbuddy", uid)
    assert account is not None
    return account


def _transport(routes: dict):
    def handler(request):
        for key, resp in routes.items():
            if key in str(request.url):
                return resp
        raise AssertionError(f"unexpected url: {request.url}")
    return httpx.MockTransport(handler)


_V3_MODELS = [
    {"id": "glm-5.3", "name": "GLM-5.3", "maxInputTokens": 1000000,
     "maxOutputTokens": 64000, "supportsToolCall": True, "supportsImages": False,
     "reasoning": {"supportedEfforts": ["low", "high", "max"],
                   "defaultEffort": "high", "canDisableThinking": False}},
    {"id": "fast-model", "name": "快速", "maxInputTokens": 300000,
     "maxOutputTokens": 48000, "supportsToolCall": True, "supportsReasoning": True,
     "reasoning": {"effort": "medium", "summary": "auto"}},
    # 过滤项：
    {"id": "hunyuan-3b", "name": "hunyuan-3b", "maxOutputTokens": 256},
    {"id": "hunyuan-image-alpha", "name": "图", "tags": ["text-to-image"]},
    {"id": "codewise-rewrite", "name": "补全", "maxOutputTokens": 256},
    {"id": "completion-gf", "name": "completion-gf", "maxOutputTokens": 8192,
     "supportsToolCall": True},  # 前缀 completion- 过滤
]

_ENT_MODELS = {
    "code": 0,
    "data": {
        "agents": [{"name": "cli", "models": ["glm-5.3", "hy3-x"]}],
        "models": [
            {"id": "glm-5.3", "name": "GLM-5.3", "maxInputTokens": 1000000,
             "maxOutputTokens": 64000},
            {"id": "hy3-x", "name": "Hy3-X", "maxInputTokens": 192000,
             "maxOutputTokens": 64000, "credits": "x0.05"},
            {"id": "space-bunny", "name": "Space Bunny", "maxInputTokens": 1000000,
             "maxOutputTokens": 64000},  # 不在 cli 白名单 → 过滤
        ],
    },
}


def test_fetch_union_v3_primary_enterprise_supplement(data_dir):
    """并集：v3 为主（glm-5.3 字段以 v3 为准）、企业端点补缺（hy3-x）、
    agents[cli] 白名单过滤（space-bunny 剔除）、非对话模型过滤。"""
    _run(_seed_account())
    t = _transport({
        catalog.V3_CONFIG_PATH: httpx.Response(200, json={
            "code": 0, "data": {"models": _V3_MODELS}}),
        catalog.ENTERPRISE_MODELS_PATH: httpx.Response(200, json=_ENT_MODELS),
    })
    models = _run(catalog.load_models(force=True, transport=t))
    names = [m["name"] for m in models]
    assert "glm-5.3" in names and "fast-model" in names
    assert "hy3-x" in names          # 企业端点补缺
    assert "space-bunny" not in names  # cli 白名单外
    assert "hunyuan-3b" not in names
    assert "hunyuan-image-alpha" not in names
    assert "codewise-rewrite" not in names
    assert "completion-gf" not in names
    # v3 优先：glm-5.3 的 reasoning 档位来自 v3（企业端点无该字段）
    glm = next(m for m in models if m["name"] == "glm-5.3")
    assert [lv["level"] for lv in
            glm["completion_options"]["thinkingLevels"]] == ["low", "high", "max"]


def test_fetch_v3_only_when_enterprise_fails(data_dir):
    """企业端点 500：v3 单路可用（降级不抛）。"""
    _run(_seed_account())

    def handler(request):
        if catalog.V3_CONFIG_PATH in str(request.url):
            return httpx.Response(200, json={"code": 0, "data": {"models": _V3_MODELS}})
        return httpx.Response(500, text="boom")

    models = _run(catalog.load_models(force=True, transport=httpx.MockTransport(handler)))
    assert {m["name"] for m in models} >= {"glm-5.3", "fast-model"}


def test_both_paths_fail_raises(data_dir):
    """两路全败：抛错（由注册表/路由层兜底为目录空）。"""
    _run(_seed_account())
    t = _transport({
        catalog.V3_CONFIG_PATH: httpx.Response(500, text="x"),
        catalog.ENTERPRISE_MODELS_PATH: httpx.Response(500, text="y"),
    })
    with pytest.raises(RuntimeError):
        _run(catalog.load_models(force=True, transport=t))


def test_no_account_returns_empty(data_dir):
    assert _run(catalog.load_models(force=True)) == []


def test_cache_hits_within_ttl(data_dir):
    """TTL 内二次调用不触网（缓存命中）；force 绕过缓存。"""
    _run(_seed_account())
    calls: list[str] = []

    def handler(request):
        calls.append(str(request.url))
        if catalog.V3_CONFIG_PATH in str(request.url):
            return httpx.Response(200, json={"code": 0, "data": {"models": _V3_MODELS}})
        return httpx.Response(200, json=_ENT_MODELS)

    t = httpx.MockTransport(handler)
    first = _run(catalog.load_models(force=True, transport=t))
    n_after_first = len(calls)
    second = _run(catalog.load_models(transport=t))  # 不 force → 缓存
    assert len(calls) == n_after_first
    assert [m["name"] for m in second] == [m["name"] for m in first]


def test_negative_cache_on_empty(data_dir, monkeypatch):
    """空目录结果负缓存：TTL 内不再触网（但 force 仍可重拉）。"""
    _run(_seed_account())

    def handler(request):
        if catalog.V3_CONFIG_PATH in str(request.url):
            return httpx.Response(200, json={"code": 0, "data": {"models": []}})
        return httpx.Response(200, json={"code": 0, "data": {"agents": [], "models": []}})

    t = httpx.MockTransport(handler)
    assert _run(catalog.load_models(force=True, transport=t)) == []
    assert _run(catalog.load_models(transport=t)) == []


def test_entry_fields_mapping(data_dir):
    """条目映射：裸名/标题/上下文/思考档位/多模态。"""
    _run(_seed_account())
    t = _transport({
        catalog.V3_CONFIG_PATH: httpx.Response(200, json={
            "code": 0, "data": {"models": _V3_MODELS}}),
        catalog.ENTERPRISE_MODELS_PATH: httpx.Response(200, json={
            "code": 0, "data": {"agents": [], "models": []}}),
    })
    models = _run(catalog.load_models(force=True, transport=t))
    glm = next(m for m in models if m["name"] == "glm-5.3")
    assert glm["title"] == "GLM-5.3"
    assert glm["context_window"] == 1000000
    assert glm["completion_options"]["thinkingEnabled"] is True
    assert glm["completion_options"]["defaultEffort"] == "high"
    assert glm["completion_options"]["canDisableThinking"] is False


def test_non_chat_filter_rules():
    assert catalog._non_chat_model({"id": "nes-x"}) is True
    assert catalog._non_chat_model({"id": "completion-gf"}) is True
    assert catalog._non_chat_model({"id": "codewise-jump"}) is True
    assert catalog._non_chat_model({"id": "m", "maxOutputTokens": 256}) is True
    assert catalog._non_chat_model({"id": "m", "maxOutputTokens": 8192}) is False
    assert catalog._non_chat_model({"id": "m", "tags": ["text-to-image"]}) is True
    assert catalog._non_chat_model({"id": "glm-5.3"}) is False


def test_non_chat_filter_generation_tags():
    """图/视频生成 tag 族扩展（2026-10-16 对齐参考实现）：seedance 视频、gpt-image 系列。"""
    assert catalog._non_chat_model({"id": "m", "tags": ["image-to-image"]}) is True
    assert catalog._non_chat_model({"id": "m", "tags": ["text-to-video"]}) is True
    assert catalog._non_chat_model({"id": "m", "tags": ["image-to-video"]}) is True
    assert catalog._non_chat_model({"id": "m", "tags": ["chat"]}) is False


# ───────── 国际版三路 UA 并集（2026-10-16 对齐参考实现 global_models.go） ─────────

_GLOBAL_DESKTOP = [
    {"id": "gpt-6-sol", "name": "GPT-6 Sol", "maxInputTokens": 400000,
     "maxOutputTokens": 128000, "supportsToolCall": True},
    {"id": "glm-5.3", "name": "GLM-5.3（桌面档）", "maxInputTokens": 1000000,
     "maxOutputTokens": 64000},
]
_GLOBAL_IDE = [
    {"id": "glm-5.3", "name": "GLM-5.3（IDE 档）", "maxInputTokens": 1,
     "maxOutputTokens": 64000},
    {"id": "o4-mini", "name": "o4-mini", "maxInputTokens": 200000,
     "maxOutputTokens": 64000},
]
_GLOBAL_CLI = [
    {"id": "deepseek-v4.1-flash", "name": "DeepSeek", "maxInputTokens": 128000,
     "maxOutputTokens": 64000},
    {"id": "kimi-k2.8-preview", "name": "Kimi", "maxInputTokens": 128000,
     "maxOutputTokens": 64000},
]


async def _seed_global_account(uid="u_cat_g"):
    await store.upsert_account("workbuddy-global", {
        "uid": uid, "access_token": "at", "refresh_token": "rt",
        "expires_at": 9_999_999_999_999, "domain": "www.workbuddy.ai"})
    account = await store.find_account("workbuddy-global", uid)
    assert account is not None
    return account


def _global_handler(seen_uas: list):
    def handler(request):
        if catalog.V3_CONFIG_PATH in str(request.url):
            ua = request.headers.get("User-Agent", "")
            seen_uas.append(ua)
            if ua == catalog._IDE_UA:
                models = _GLOBAL_IDE
            elif ua == catalog._CLI_UA:
                models = _GLOBAL_CLI
            else:
                models = _GLOBAL_DESKTOP
            return httpx.Response(200, json={"code": 0, "data": {"models": models}})
        return httpx.Response(200, json={"code": 0, "data": {"agents": [], "models": []}})
    return handler


def test_global_v3_three_routes_union_desktop_priority(data_dir):
    """global：三路 UA 并集；桌面档字段权威（同 id 取桌面档条目），IDE/CLI 补缺失 id。"""
    _run(_seed_global_account())
    seen: list[str] = []
    t = httpx.MockTransport(_global_handler(seen))
    models = _run(catalog.load_models(force=True, transport=t, profile=REALM_GLOBAL))
    names = {m["name"] for m in models}
    assert {"gpt-6-sol", "glm-5.3", "o4-mini", "deepseek-v4.1-flash",
            "kimi-k2.8-preview"} <= names
    glm = next(m for m in models if m["name"] == "glm-5.3")
    assert glm["title"] == "GLM-5.3（桌面档）"  # 主路字段权威
    assert len(seen) == 3  # 三路各探测一次


def test_global_v3_route_partial_failure(data_dir):
    """单路失败不拖累其余路（WARN 降级），结果仍为可用路并集。"""
    _run(_seed_global_account())

    def handler(request):
        if catalog.V3_CONFIG_PATH in str(request.url):
            ua = request.headers.get("User-Agent", "")
            if ua == catalog._IDE_UA:
                return httpx.Response(500, text="boom")
            models = _GLOBAL_CLI if ua == catalog._CLI_UA else _GLOBAL_DESKTOP
            return httpx.Response(200, json={"code": 0, "data": {"models": models}})
        return httpx.Response(200, json={"code": 0, "data": {"agents": [], "models": []}})

    models = _run(catalog.load_models(force=True, transport=httpx.MockTransport(handler),
                                      profile=REALM_GLOBAL))
    names = {m["name"] for m in models}
    assert {"gpt-6-sol", "deepseek-v4.1-flash"} <= names
    assert "o4-mini" not in names


def test_cn_v3_single_route_and_explicit_ua_override(data_dir, monkeypatch):
    """CN 单路（参考实现 CN 侧亦单路）；显式 wb_user_agent 覆盖同样退化为单路。"""
    _run(_seed_account())
    calls: list[str] = []

    def handler(request):
        if catalog.V3_CONFIG_PATH in str(request.url):
            calls.append(request.headers.get("User-Agent", ""))
            return httpx.Response(200, json={"code": 0, "data": {"models": _V3_MODELS}})
        return httpx.Response(200, json=_ENT_MODELS)

    t = httpx.MockTransport(handler)
    _run(catalog.load_models(force=True, transport=t))
    assert len(calls) == 1 and "WorkBuddy/" in calls[0]
    monkeypatch.setattr(settings, "wb_user_agent", "MyUA/1.0")
    calls.clear()
    _run(catalog.load_models(force=True, transport=t))
    assert calls == ["MyUA/1.0"]
