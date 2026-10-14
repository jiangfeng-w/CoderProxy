"""模型前缀命名空间与 adapter 注册表骨架 keyless 单测（§4.1 / §3.2）。

覆盖：
- model_ref：裸名 / 牛码 / WorkBuddy / 大小写 / 未知前缀 / 空白 / 多斜杠切分；
- 白名单与思考默认的读时归一（storage 层，含存量裸名兼容）；
- routes 路由：裸名与 `牛码/…` 全名都命中牛码链路（Ta3Provider 只拿裸名）；
- 未注册前缀（WorkBuddy 聊天尚未落地 / 未知供应商）→ 404 明确报错；
- /v1/models 全名暴露 + 白名单过滤全名语义。
"""
import asyncio

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from relay import model_ref, routes as routes_mod, storage
from relay.config import settings
from relay.routes import app as relay_app


def _run(coro):
    return asyncio.run(coro)


def _auth():
    return {"Authorization": "Bearer prefix-test-key"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    settings.relay_api_key = "prefix-test-key"

    async def _serving_on():
        return True

    monkeypatch.setattr(routes_mod, "_serving", _serving_on)
    with TestClient(relay_app, raise_server_exceptions=False) as c:
        yield c
    settings.relay_api_key = ""


_MODELS = [
    {"name": "glm-5.3", "api_key": "k", "base_url": "b", "anthropic": False},
    {"name": "kimi-k3", "api_key": "k", "base_url": "b", "anthropic": True},
]


# ─────────────────────────── model_ref 解析 ───────────────────────────

def test_parse_bare_name_defaults_to_niucode():
    ref = model_ref.parse_model_ref("glm-5.3")
    assert ref.prefix == "" and ref.bare == "glm-5.3"
    assert ref.provider == "牛码" and ref.is_niucode
    assert ref.canonical == "牛码/glm-5.3"


def test_parse_prefixed_names():
    ref = model_ref.parse_model_ref("牛码/glm-5.3")
    assert ref.prefix == "牛码" and ref.bare == "glm-5.3" and ref.is_niucode
    ref = model_ref.parse_model_ref("wbcn/glm-5.3-flash")
    assert ref.prefix == "wbcn" and ref.bare == "glm-5.3-flash"
    assert not ref.is_niucode and ref.canonical == "wbcn/glm-5.3-flash"
    ref = model_ref.parse_model_ref("wbg/glm-5.3-flash")
    assert ref.prefix == "wbg" and ref.canonical == "wbg/glm-5.3-flash"
    # legacy 读兼容（§5）：旧 WorkBuddy/ 归一为 wbcn/（大小写不敏感）
    ref = model_ref.parse_model_ref("WorkBuddy/glm-5.3-flash")
    assert ref.prefix == "wbcn" and ref.canonical == "wbcn/glm-5.3-flash"


def test_parse_case_insensitive_prefix():
    assert model_ref.parse_model_ref("wbcn/x").prefix == "wbcn"
    assert model_ref.parse_model_ref("WBCN/x").prefix == "wbcn"
    assert model_ref.parse_model_ref("WBG/x").prefix == "wbg"
    assert model_ref.parse_model_ref("workbuddy/x").prefix == "wbcn"  # legacy 大小写不敏感
    assert model_ref.parse_model_ref("WORKBUDDY/x").prefix == "wbcn"
    assert model_ref.parse_model_ref("牛码/x").prefix == "牛码"


def test_parse_unknown_prefix_raises_strict():
    with pytest.raises(model_ref.UnknownProviderError):
        model_ref.parse_model_ref("基元律动/glm-5.3")


def test_parse_lenient_keeps_unknown_prefix():
    ref = model_ref.parse_model_ref_lenient("基元律动/glm-5.3-flash")
    assert ref.prefix == "基元律动" and ref.bare == "glm-5.3-flash"


def test_parse_edge_shapes():
    # 空白 → 裸名空串
    assert model_ref.parse_model_ref_lenient("  ").bare == ""
    # 空头（/x）与空尾（牛码/）→ 均判为裸名（前缀不可用，原样整串）
    assert model_ref.parse_model_ref_lenient("/x").bare == "/x"
    assert model_ref.parse_model_ref_lenient("牛码/").bare == "牛码/"
    # 三段式：按第一个斜杠切分，后段保留
    ref = model_ref.parse_model_ref_lenient("牛码/a/b")
    assert ref.prefix == "牛码" and ref.bare == "a/b"


def test_canonical_key():
    assert model_ref.canonical_key("glm-5.3") == "牛码/glm-5.3"
    assert model_ref.canonical_key("牛码/glm-5.3") == "牛码/glm-5.3"
    assert model_ref.canonical_key("wbcn/x") == "wbcn/x"
    assert model_ref.canonical_key("wbg/x") == "wbg/x"
    # legacy 读兼容：旧键读归一为新规范名（落盘不改，后续保存自然写新键）
    assert model_ref.canonical_key("WorkBuddy/x") == "wbcn/x"
    # 未知前缀：宽容原样（不炸读路径）
    assert model_ref.canonical_key("基元律动/x") == "基元律动/x"


def test_normalize_whitelist():
    assert model_ref.normalize_whitelist(["glm-x", "牛码/y", "wbcn/z", "wbg/w"]) == [
        "牛码/glm-x", "牛码/y", "wbcn/z", "wbg/w"]
    # legacy 键读归一命中新键
    assert model_ref.normalize_whitelist(["WorkBuddy/z"]) == ["wbcn/z"]
    assert model_ref.normalize_whitelist(["__none__"]) == ["__none__"]


# ─────────────────────────── storage：读时归一 ───────────────────────────

def test_whitelist_legacy_bare_names_normalized_on_read(tmp_path, monkeypatch):
    """存量落盘裸名（M4 老数据）读取归一为全名，无需迁移写盘。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(storage.set_model_whitelist(["glm-x"]))
    # 直接改落盘文件模拟存量数据（绕过写路径归一）
    import json
    from pathlib import Path
    p = Path(tmp_path) / "relay_state.json"
    state = json.loads(p.read_text("utf-8"))
    state["config"]["model_whitelist"] = ["glm-x", "kimi-y"]
    p.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    assert _run(storage.get_model_whitelist()) == ["牛码/glm-x", "牛码/kimi-y"]
    assert _run(storage.is_model_enabled("牛码/glm-x")) is True


def test_thinking_defaults_legacy_bare_names_normalized_on_read(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    import json
    from pathlib import Path
    p = Path(tmp_path) / "relay_state.json"
    _run(storage.save_thinking_defaults({"x": "low"}))
    state = json.loads(p.read_text("utf-8"))
    state["config"]["thinking_defaults"] = {"glm-5.3": "high"}
    p.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    assert _run(storage.get_thinking_defaults()) == {"牛码/glm-5.3": "high"}


# ─────────────────────────── routes：路由与 /v1/models ───────────────────────────

def test_ensure_model_bare_and_prefixed_both_hit_niucode(client):
    _run(storage.save_models(_MODELS))
    assert _run(routes_mod._ensure_model("glm-5.3"))["name"] == "glm-5.3"
    assert _run(routes_mod._ensure_model("牛码/glm-5.3"))["name"] == "glm-5.3"


def test_ensure_model_unknown_prefix_404(client, monkeypatch):
    _run(storage.save_models(_MODELS))

    async def _no_sync():
        return []

    monkeypatch.setattr(routes_mod.auth_flow, "sync_models", _no_sync)
    with pytest.raises(HTTPException) as ei:
        _run(routes_mod._ensure_model("基元律动/glm-5.3"))
    assert ei.value.status_code == 404
    assert "未知供应商" in ei.value.detail


def test_ensure_model_workbuddy_no_accounts_503(client, monkeypatch):
    """WorkBuddy 已注册但账号池为空：503（§4.3 引导到供应商页），非 404 未知模型。

    双域：`wbcn/…` 与 legacy `WorkBuddy/…` 同判（读归一）。
    """
    _run(storage.save_models(_MODELS))

    async def _no_sync():
        return []

    monkeypatch.setattr(routes_mod.auth_flow, "sync_models", _no_sync)
    for name in ("wbcn/glm-5.3-flash", "WorkBuddy/glm-5.3-flash"):
        with pytest.raises(routes_mod.PlatformUnavailableError) as ei:
            _run(routes_mod._ensure_model(name))
        assert ei.value.retry_after == "60"
        assert "供应商页" in ei.value.detail


def test_bare_name_provider_gets_bare_model(client, monkeypatch):
    """牛码链路：Ta3Provider 收到裸名（前缀剥离）——裸名请求不回归。"""
    _run(storage.save_models(_MODELS))
    built: list = []

    def _fake_build(model, model_name, ctx=None):
        built.append(model_name)
        return _FakeOkProvider()

    monkeypatch.setattr(routes_mod, "build_adapter", _fake_build)
    r = client.post("/v1/chat/completions",
                    json={"model": "glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
                    headers=_auth())
    assert r.status_code == 200
    assert built == ["glm-5.3"]
    assert r.json()["model"] == "glm-5.3"  # 回显原请求名


def test_prefixed_request_strips_prefix_for_provider(client, monkeypatch):
    _run(storage.save_models(_MODELS))
    built: list = []

    def _fake_build(model, model_name, ctx=None):
        built.append(model_name)
        return _FakeOkProvider()

    monkeypatch.setattr(routes_mod, "build_adapter", _fake_build)
    r = client.post("/v1/chat/completions",
                    json={"model": "牛码/glm-5.3", "messages": [{"role": "user", "content": "hi"}]},
                    headers=_auth())
    assert r.status_code == 200
    # build_adapter 收到请求原名（内部自行剥前缀），下游 provider 拿裸名由 build_provider 保证
    assert built == ["牛码/glm-5.3"]
    assert r.json()["model"] == "牛码/glm-5.3"  # 回显全名（agent 视角一致）


def test_models_endpoint_exposes_canonical_names(client):
    _run(storage.save_models(_MODELS))
    data = client.get("/v1/models", headers=_auth()).json()["data"]
    assert [m["id"] for m in data] == ["牛码/glm-5.3", "牛码/kimi-k3"]
    # 单模型端点（全名 path 含斜杠）：{model_id:path} 捕获
    one = client.get("/v1/models/牛码/glm-5.3", headers=_auth())
    assert one.status_code == 200 and one.json()["id"] == "牛码/glm-5.3"


def test_whitelist_full_name_filter(client):
    _run(storage.save_models(_MODELS))
    _run(storage.set_model_whitelist(["wbcn/glm-5.3-flash"]))
    assert client.get("/v1/models", headers=_auth()).json()["data"] == []


# ─────────────────────────── 注册表路由（WorkBuddy 链路） ───────────────────────────

def test_workbuddy_registered_on_import():
    """导入 relay.routes 后注册表应有双域前缀的目录加载器与 adapter 工厂。"""
    from relay import adapter_registry, routes  # noqa: F401（导入即注册）
    for prefix in ("wbcn", "wbg"):
        assert adapter_registry.has_provider(prefix)
        assert adapter_registry.adapter_factory(prefix) is not None


def test_registry_routed_workbuddy_endpoint(client, monkeypatch):
    """`wbcn/…` 前缀 → 注册表工厂构造 adapter 并转发（假 adapter，不触网）；
    legacy `WorkBuddy/…` 读归一后等价命中（§5）。"""
    from relay import adapter_registry

    calls: list[str] = []

    class _FakeWb:
        def __init__(self, bare):
            self._bare = bare

        async def chat(self, request):
            from app.models.schemas import ChatResponse, Usage
            calls.append(self._bare)
            return ChatResponse(content="wb-ok", finish_reason="stop",
                                usage=Usage(prompt_tokens=1, completion_tokens=1,
                                            total_tokens=2))

    async def _dir():
        # 补思考档位：模型列表过时治理后 WB 无档位默认隐藏（否则本例被可见性门控拦成 404）
        return [{"name": "glm-5.3-flash", "context_window": 1000000,
                 "completion_options": {"thinkingLevels": [{"level": "low"}, {"level": "high"}]}}]

    adapter_registry.register_provider("wbcn", directory=_dir,
                                       factory=lambda bare, **kw: _FakeWb(bare))
    try:
        # 白名单为空（全部启用）；目录来自注册表
        r = client.post("/v1/chat/completions",
                        json={"model": "wbcn/glm-5.3-flash",
                              "messages": [{"role": "user", "content": "hi"}]},
                        headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["choices"][0]["message"]["content"] == "wb-ok"
        assert body["model"] == "wbcn/glm-5.3-flash"  # 回显全名
        assert calls == ["glm-5.3-flash"]  # 上游拿裸名
        # legacy 旧前缀读兼容：WorkBuddy/x 归一 wbcn/x 照常路由（§5）
        r2 = client.post("/v1/chat/completions",
                         json={"model": "WorkBuddy/glm-5.3-flash",
                               "messages": [{"role": "user", "content": "hi"}]},
                         headers=_auth())
        assert r2.status_code == 200
        assert calls == ["glm-5.3-flash", "glm-5.3-flash"]
        # /v1/models 合并出现（新前缀 id + 牛码目录；不再输出旧前缀）
        _run(storage.save_models(_MODELS))
        ids = [m["id"] for m in client.get("/v1/models", headers=_auth()).json()["data"]]
        assert "wbcn/glm-5.3-flash" in ids and "牛码/glm-5.3" in ids
        assert all(not i.startswith("WorkBuddy/") for i in ids)
    finally:
        adapter_registry.unregister_provider("wbcn")
        # 恢复真实注册（导入 routes 时已注册；unregister 后需还原以防其它测试受影响）
        from relay.platforms.workbuddy.chat import factory_for
        from relay.platforms.workbuddy.realms import REALM_CN
        adapter_registry.register_provider("wbcn", factory=factory_for(REALM_CN))


def test_no_available_account_maps_503_with_retry_after(client, monkeypatch):
    """账号池不可用 → 503 + Retry-After（§4.3）。"""
    from relay import adapter_registry
    from relay.platforms.workbuddy.chat import NoAvailableAccountError

    class _EmptyWb:
        async def chat(self, request):
            raise NoAvailableAccountError("无可用 WorkBuddy 账号")

        async def stream_structured(self, request):
            raise NoAvailableAccountError("无可用 WorkBuddy 账号")
            yield  # pragma: no cover

    async def _dir():
        # 补思考档位（模型列表过时治理：无档位默认隐藏，会让本例在 503 前先撞 404）
        return [{"name": "m", "completion_options":
                 {"thinkingLevels": [{"level": "low"}]}}]

    adapter_registry.register_provider("wbcn", directory=_dir,
                                       factory=lambda bare, **kw: _EmptyWb())
    try:
        r = client.post("/v1/chat/completions",
                        json={"model": "wbcn/m",
                              "messages": [{"role": "user", "content": "hi"}]},
                        headers=_auth())
        assert r.status_code == 503
        assert r.headers.get("Retry-After") == "60"
        assert r.json()["error"]["type"] == "no_available_account"
    finally:
        adapter_registry.unregister_provider("wbcn")
        from relay.platforms.workbuddy.chat import factory_for
        from relay.platforms.workbuddy.realms import REALM_CN
        adapter_registry.register_provider("wbcn", factory=factory_for(REALM_CN))


class _FakeOkProvider:
    _model_name = "fake"

    async def chat(self, request):
        from app.models.schemas import ChatResponse, Usage
        return ChatResponse(content="ok", finish_reason="stop",
                            usage=Usage(prompt_tokens=1, completion_tokens=1,
                                        total_tokens=2))

    async def stream_structured(self, request):
        yield {"type": "done", "content": "ok", "thinking": None, "tool_calls": [],
               "finish_reason": "stop",
               "usage": None, "raw_tail": None}


# ─────────────────── 方案 A：工具伪装只对牛码（2026-10-09 修复） ───────────────────

_GREP_TOOL = {"type": "function",
              "function": {"name": "Grep", "parameters": {"type": "object",
                                                          "properties": {}}}}


def _tool_disguise_count() -> int:
    return routes_mod.monitor.stats().get("tool_disguise", 0)


class _CaptureToolsProvider(_FakeOkProvider):
    """捕获 chat 收到的 request.tools（伪装门控断言用）。"""

    def __init__(self):
        self.tools = None

    async def chat(self, request):
        self.tools = request.tools
        return await super().chat(request)


def test_niucode_tools_still_disguised(client, monkeypatch):
    """牛码链路不受门控影响：Grep → Search（原生 schema），监控计数照常。"""
    _run(storage.save_models(_MODELS))
    cap = _CaptureToolsProvider()
    monkeypatch.setattr(routes_mod, "build_provider", lambda model, name, ctx=None: cap)
    before = _tool_disguise_count()
    r = client.post("/v1/chat/completions",
                    json={"model": "glm-5.3",
                          "messages": [{"role": "user", "content": "hi"}],
                          "tools": [_GREP_TOOL]},
                    headers=_auth())
    assert r.status_code == 200
    assert [t["function"]["name"] for t in cap.tools] == ["Search"]  # 已伪装为牛码原生名
    assert _tool_disguise_count() == before + 1


def test_workbuddy_tools_pass_through_unmodified(client, monkeypatch):
    """WorkBuddy 链路：工具原名直达 adapter（无 ctx），不产生 tool_disguise 监控。

    修复前：无条件跑 build_disguise_context 且 adapter 分支不接收 ctx →
    `Grep→Search`、`Skill→ReadSkill` 改名直发上游、无任何还原路径。
    """
    from relay import adapter_registry

    captured: dict = {}

    class _FakeWb:
        def __init__(self, bare):
            self._bare = bare

        async def chat(self, request):
            from app.models.schemas import ChatResponse, Usage
            captured["tools"] = request.tools
            return ChatResponse(content="wb-ok", finish_reason="stop",
                                usage=Usage(prompt_tokens=1, completion_tokens=1,
                                            total_tokens=2))

    async def _dir():
        # 补思考档位（模型列表过时治理：无档位默认隐藏，会让本例被可见性门控拦成 404）
        return [{"name": "glm-5.3-flash", "completion_options":
                 {"thinkingLevels": [{"level": "low"}]}}]

    adapter_registry.register_provider("wbcn", directory=_dir,
                                       factory=lambda bare, **kw: _FakeWb(bare))
    try:
        before = _tool_disguise_count()
        r = client.post("/v1/chat/completions",
                        json={"model": "wbcn/glm-5.3-flash",
                              "messages": [{"role": "user", "content": "hi"}],
                              "tools": [_GREP_TOOL]},
                        headers=_auth())
        assert r.status_code == 200
        assert [t["function"]["name"] for t in captured["tools"]] == ["Grep"]  # 原名透传
        assert _tool_disguise_count() == before  # 无伪装监控（ctx=None 不 emit）
    finally:
        adapter_registry.unregister_provider("wbcn")
        from relay.platforms.workbuddy.chat import factory_for
        from relay.platforms.workbuddy.realms import REALM_CN
        adapter_registry.register_provider("wbcn", factory=factory_for(REALM_CN))


# ─────────────── 思考强度透传档位：routes 层透传/强制覆写 ───────────────

class _CaptureRequestProvider(_FakeOkProvider):
    """捕获 chat 收到的 ChatRequest（思考档位透传/强制断言用）。"""

    def __init__(self):
        self.request = None

    async def chat(self, request):
        self.request = request
        return await super().chat(request)


def _post_chat(client, **extra):
    return client.post("/v1/chat/completions",
                       json={"model": "glm-5.3",
                             "messages": [{"role": "user", "content": "hi"}], **extra},
                       headers=_auth())


def test_thinking_forced_gear_overrides_agent(client, monkeypatch):
    """强制档：GUI 配置的具体档覆写 agent 传参（含冲突档位）——routes 层统一覆写。"""
    _run(storage.save_models(_MODELS))
    _run(storage.save_thinking_defaults({"牛码/glm-5.3": "low"}))
    cap = _CaptureRequestProvider()
    monkeypatch.setattr(routes_mod, "build_provider", lambda model, name, ctx=None: cap)
    r = _post_chat(client, reasoning_effort="max")
    assert r.status_code == 200
    assert cap.request.thinking is True
    assert cap.request.reasoning_effort == "low"  # 无视 agent 的 max


def test_thinking_forced_none_overrides_open_intent(client, monkeypatch):
    """强制关：agent 传了开+档位也一律覆写为 effort=none（ta3 关路径接住）。"""
    _run(storage.save_models(_MODELS))
    _run(storage.save_thinking_defaults({"glm-5.3": "none"}))  # 裸名键 → 读时归一牛码/
    cap = _CaptureRequestProvider()
    monkeypatch.setattr(routes_mod, "build_provider", lambda model, name, ctx=None: cap)
    r = _post_chat(client, thinking=True, reasoning_effort="high")
    assert r.status_code == 200
    assert cap.request.thinking is True
    assert cap.request.reasoning_effort == "none"


def test_thinking_passthrough_default_keeps_agent_params(client, monkeypatch):
    """透传（默认，未配置）：agent 表态原样保留，relay 不碰 ChatRequest。"""
    _run(storage.save_models(_MODELS))
    cap = _CaptureRequestProvider()
    monkeypatch.setattr(routes_mod, "build_provider", lambda model, name, ctx=None: cap)
    r = _post_chat(client, thinking=True, reasoning_effort="max")
    assert r.status_code == 200
    assert cap.request.thinking is True
    assert cap.request.reasoning_effort == "max"  # 不降级不改写


def test_thinking_passthrough_zero_intent_no_injection(client, monkeypatch):
    """透传 + agent 零表态 → 零注入（行为翻转：旧「兜底 none 强制关」已退役）。"""
    _run(storage.save_models(_MODELS))
    cap = _CaptureRequestProvider()
    monkeypatch.setattr(routes_mod, "build_provider", lambda model, name, ctx=None: cap)
    r = _post_chat(client)
    assert r.status_code == 200
    assert cap.request.thinking is None
    assert cap.request.reasoning_effort is None


def test_thinking_explicit_passthrough_value_keeps_agent(client, monkeypatch):
    """显式配置 "passthrough" → 同默认透传（值域扩展，覆写不触发）。"""
    _run(storage.save_models(_MODELS))
    _run(storage.save_thinking_defaults({"牛码/glm-5.3": "passthrough"}))
    cap = _CaptureRequestProvider()
    monkeypatch.setattr(routes_mod, "build_provider", lambda model, name, ctx=None: cap)
    r = _post_chat(client, reasoning_effort="high")
    assert r.status_code == 200
    assert cap.request.reasoning_effort == "high"
