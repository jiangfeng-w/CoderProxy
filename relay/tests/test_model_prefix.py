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
    ref = model_ref.parse_model_ref("WorkBuddy/glm-5.3-flash")
    assert ref.prefix == "WorkBuddy" and ref.bare == "glm-5.3-flash"
    assert not ref.is_niucode and ref.canonical == "WorkBuddy/glm-5.3-flash"


def test_parse_case_insensitive_prefix():
    assert model_ref.parse_model_ref("workbuddy/x").prefix == "WorkBuddy"
    assert model_ref.parse_model_ref("WORKBUDDY/x").prefix == "WorkBuddy"
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
    assert model_ref.canonical_key("WorkBuddy/x") == "WorkBuddy/x"
    # 未知前缀：宽容原样（不炸读路径）
    assert model_ref.canonical_key("基元律动/x") == "基元律动/x"


def test_normalize_whitelist():
    assert model_ref.normalize_whitelist(["glm-x", "牛码/y", "WorkBuddy/z"]) == [
        "牛码/glm-x", "牛码/y", "WorkBuddy/z"]
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


def test_ensure_model_workbuddy_not_loaded_404(client, monkeypatch):
    """WorkBuddy 聊天 adapter 未注册（步骤 2 前）：报未知供应商而非误路由牛码。"""
    _run(storage.save_models(_MODELS))

    async def _no_sync():
        return []

    monkeypatch.setattr(routes_mod.auth_flow, "sync_models", _no_sync)
    with pytest.raises(HTTPException) as ei:
        _run(routes_mod._ensure_model("WorkBuddy/glm-5.3-flash"))
    assert ei.value.status_code == 404


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
    _run(storage.set_model_whitelist(["WorkBuddy/glm-5.3-flash"]))
    assert client.get("/v1/models", headers=_auth()).json()["data"] == []


# ─────────────────────────── 注册表路由（WorkBuddy 链路） ───────────────────────────

def test_registry_routed_workbuddy_endpoint(client, monkeypatch):
    """`WorkBuddy/…` 前缀 → 注册表工厂构造 adapter 并转发（假 adapter，不触网）。"""
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
        return [{"name": "glm-5.3-flash", "context_window": 1000000}]

    adapter_registry.register_provider("WorkBuddy", directory=_dir,
                                       factory=lambda bare, **kw: _FakeWb(bare))
    try:
        # 白名单为空（全部启用）；目录来自注册表
        r = client.post("/v1/chat/completions",
                        json={"model": "WorkBuddy/glm-5.3-flash",
                              "messages": [{"role": "user", "content": "hi"}]},
                        headers=_auth())
        assert r.status_code == 200
        body = r.json()
        assert body["choices"][0]["message"]["content"] == "wb-ok"
        assert body["model"] == "WorkBuddy/glm-5.3-flash"  # 回显全名
        assert calls == ["glm-5.3-flash"]  # 上游拿裸名
        # /v1/models 合并出现（全名 id + 牛码目录）
        _run(storage.save_models(_MODELS))
        ids = [m["id"] for m in client.get("/v1/models", headers=_auth()).json()["data"]]
        assert "WorkBuddy/glm-5.3-flash" in ids and "牛码/glm-5.3" in ids
    finally:
        adapter_registry.unregister_provider("WorkBuddy")
        # 恢复真实注册（导入 routes 时已注册；unregister 后需还原以防其它测试受影响）
        from relay.platforms.workbuddy.chat import factory
        adapter_registry.register_provider("WorkBuddy", factory=factory)


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
        return [{"name": "m"}]

    adapter_registry.register_provider("WorkBuddy", directory=_dir,
                                       factory=lambda bare, **kw: _EmptyWb())
    try:
        r = client.post("/v1/chat/completions",
                        json={"model": "WorkBuddy/m",
                              "messages": [{"role": "user", "content": "hi"}]},
                        headers=_auth())
        assert r.status_code == 503
        assert r.headers.get("Retry-After") == "60"
        assert r.json()["error"]["type"] == "no_available_account"
    finally:
        adapter_registry.unregister_provider("WorkBuddy")
        from relay.platforms.workbuddy.chat import factory
        adapter_registry.register_provider("WorkBuddy", factory=factory)


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
