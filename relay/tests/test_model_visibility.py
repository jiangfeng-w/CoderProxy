"""模型列表过时治理（模型可见性）keyless 单测（§4.2/§4.3/§4.4 验收）。

覆盖：
- storage：model_visibility 读写/归一（裸名→全名）/非法值丢弃（损坏回退）；
- /v1/auth/config：非法值 400 不落盘；model_visibility 与 model_whitelist 同批提交均落盘；
- effective visibility 判定：WB 无档位默认隐藏 / 有档位默认显示 / 覆盖 show|hide 优先 /
  非 WB（牛码/自定义）默认恒可见（覆盖仍生效）；
- /v1/models?all=1 每条带 visible；/v1/models（all=0）= 可见性 ∩ 白名单；
  单模型端点不过滤可见性；
- chat 门控联动：隐藏直调 404（与白名单同文案）、probe 豁免、显示+启用可调、
  显示+禁用 404。

不触网：WorkBuddy 用注册表假 provider（test_model_prefix 同款），牛码目录落 tmp。
"""
import asyncio
import json
from pathlib import Path

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from relay import adapter_registry, providers_custom, routes as routes_mod, storage
from relay.config import settings
from relay.routes import app as relay_app


def _run(coro):
    return asyncio.run(coro)


def _auth():
    return {"Authorization": "Bearer vis-test-key"}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    settings.relay_api_key = "vis-test-key"

    async def _serving_on():
        return True

    monkeypatch.setattr(routes_mod, "_serving", _serving_on)
    with TestClient(relay_app, raise_server_exceptions=False) as c:
        yield c
    settings.relay_api_key = ""


# 假 WorkBuddy 目录：wb-new 有档位（新代，默认显示）、wb-old 无档位（过时代，默认隐藏）
_WB_MODELS = [
    {"name": "wb-new", "context_window": 1000000,
     "completion_options": {"thinkingLevels": [{"level": "low"}, {"level": "high"}]}},
    {"name": "wb-old", "context_window": 128000},
]

_NIUCODE_MODELS = [
    {"name": "glm-5.3", "api_key": "k", "base_url": "b", "anthropic": False},
]


def _register_wb(models: list[dict]):
    """注册假 WorkBuddy provider（目录替身 + 假 adapter，不触网）。"""

    class _FakeWb:
        def __init__(self, bare):
            self._bare = bare

        async def chat(self, request):
            from app.models.schemas import ChatResponse, Usage
            return ChatResponse(content="wb-ok", finish_reason="stop",
                                usage=Usage(prompt_tokens=1, completion_tokens=1,
                                            total_tokens=2))

    async def _dir():
        return models

    adapter_registry.register_provider("wbcn", directory=_dir,
                                       factory=lambda bare, **kw: _FakeWb(bare))


def _restore_wb():
    """还原真实注册（导入 routes 时已注册；双域前缀一并还原）。"""
    from relay.platforms.workbuddy import chat as wb_chat
    adapter_registry.register_provider(
        "wbcn",
        directory=wb_chat._directory_for(wb_chat.REALM_CN),  # noqa: SLF001
        factory=wb_chat.factory_for(wb_chat.REALM_CN),
        availability=wb_chat._availability_for(wb_chat.REALM_CN))  # noqa: SLF001


def _post_chat(client, model, **extra):
    return client.post("/v1/chat/completions",
                       json={"model": model,
                             "messages": [{"role": "user", "content": "hi"}], **extra},
                       headers=_auth())


def _model_ref(name):
    from relay import model_ref
    return model_ref.parse_model_ref_lenient(name)


# ─────────────────────────── storage：读/写/归一 ───────────────────────────

def test_visibility_roundtrip_normalizes_keys(tmp_path, monkeypatch):
    """写入前/读出时键都归一为规范全名；裸名落盘 → 读视图归一（对齐白名单）。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    saved = _run(storage.save_model_visibility({"wb-x": "show", "wbcn/wb-y": "hide"}))
    assert saved == {"牛码/wb-x": "show", "wbcn/wb-y": "hide"}
    assert _run(storage.get_model_visibility()) == {"牛码/wb-x": "show", "wbcn/wb-y": "hide"}
    # legacy 键读归一（§5）：旧 WorkBuddy/ 键等价命中新规范键
    from pathlib import Path as _P
    import json as _json
    p = _P(tmp_path) / "relay_state.json"
    state = _json.loads(p.read_text("utf-8"))
    state["config"]["model_visibility"]["WorkBuddy/wb-legacy"] = "hide"
    p.write_text(_json.dumps(state, ensure_ascii=False), "utf-8")
    assert _run(storage.get_model_visibility())["wbcn/wb-legacy"] == "hide"


def test_visibility_invalid_values_dropped_and_corrupt_fallback(tmp_path, monkeypatch):
    """非法值键丢弃；整体损坏（非 dict）回退 {}，不炸读路径。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(storage.save_model_visibility({"wbcn/a": "show", "wbcn/b": "bogus"}))
    assert _run(storage.get_model_visibility()) == {"wbcn/a": "show"}
    # 直接改落盘文件模拟存量脏数据
    p = Path(tmp_path) / "relay_state.json"
    state = json.loads(p.read_text("utf-8"))
    state["config"]["model_visibility"] = ["not-a-dict"]
    p.write_text(json.dumps(state, ensure_ascii=False), "utf-8")
    assert _run(storage.get_model_visibility()) == {}


def test_config_api_invalid_visibility_400_not_persisted(client):
    """API 写入非法值 → 400 且不落盘（全字段先校验模式）。"""
    r = client.post("/v1/auth/config", json={"model_visibility": {"wbcn/x": "bogus"}},
                    headers=_auth())
    assert r.status_code == 400
    assert _run(storage.get_model_visibility()) == {}


def test_config_api_batch_whitelist_and_visibility(client):
    """弹窗「显示/启用联动」同批提交：一次 POST 双字段均落盘（成对写入契约）。"""
    r = client.post("/v1/auth/config",
                    json={"model_visibility": {"wbcn/wb-old": "show"},
                          "model_whitelist": ["wbcn/wb-old"]},
                    headers=_auth())
    assert r.status_code == 200
    assert r.json()["model_visibility"] == {"wbcn/wb-old": "show"}
    assert r.json()["model_whitelist"] == ["wbcn/wb-old"]
    assert _run(storage.get_model_visibility()) == {"wbcn/wb-old": "show"}


# ─────────────────────────── 判定 + /v1/models 注入/过滤 ───────────────────────────

def test_models_visible_flag_and_agent_filter(client, monkeypatch):
    """all=1 每条带 visible（默认判定）；all=0 过滤隐藏（不变式：隐藏 ⇒ 已停用）。"""
    _run(storage.save_models(_NIUCODE_MODELS))
    _register_wb(_WB_MODELS)
    try:
        full = client.get("/v1/models?all=1", headers=_auth()).json()["data"]
        vis = {m["id"]: m["visible"] for m in full}
        assert vis == {"牛码/glm-5.3": True, "wbcn/wb-new": True,
                       "wbcn/wb-old": False}
        # agent 侧：隐藏条目不出现；牛码与有档位 WB 正常
        ids = [m["id"] for m in client.get("/v1/models", headers=_auth()).json()["data"]]
        assert ids == ["牛码/glm-5.3", "wbcn/wb-new"]
    finally:
        _restore_wb()


def test_visibility_override_priority_over_default(client):
    """用户覆盖优先于默认：无档位手动 show 恢复；有档位手动 hide 隐藏。"""
    _run(storage.save_models(_NIUCODE_MODELS))
    _register_wb(_WB_MODELS)
    try:
        _run(storage.save_model_visibility({"wbcn/wb-old": "show",
                                            "wbcn/wb-new": "hide"}))
        ids = [m["id"] for m in client.get("/v1/models", headers=_auth()).json()["data"]]
        assert ids == ["牛码/glm-5.3", "wbcn/wb-old"]
        full = client.get("/v1/models?all=1", headers=_auth()).json()["data"]
        vis = {m["id"]: m["visible"] for m in full}
        assert vis["wbcn/wb-old"] is True and vis["wbcn/wb-new"] is False
    finally:
        _restore_wb()


def test_models_filter_keeps_whitelist_intersection(client):
    """all=0 = 可见性 ∩ 白名单：白名单外的可见模型同样被过滤；隐藏者即使列内也不出现。"""
    _run(storage.save_models(_NIUCODE_MODELS))
    _register_wb(_WB_MODELS)
    try:
        _run(storage.set_model_whitelist(["牛码/glm-5.3", "wbcn/wb-old"]))
        ids = [m["id"] for m in client.get("/v1/models", headers=_auth()).json()["data"]]
        assert ids == ["牛码/glm-5.3"]  # wb-old 在白名单内但被可见性过滤
    finally:
        _restore_wb()


def test_single_model_endpoint_not_visibility_filtered(client):
    """GET /v1/models/{id} 不过滤可见性（与现状不过滤白名单一致）。"""
    _register_wb(_WB_MODELS)
    try:
        r = client.get("/v1/models/wbcn/wb-old", headers=_auth())
        assert r.status_code == 200 and r.json()["id"] == "wbcn/wb-old"
    finally:
        _restore_wb()


def test_catalog_refresh_keeps_overrides_and_defaults_new_entries(client):
    """目录刷新（§6.5）：新增无档位 → 默认隐藏；已有覆盖不漂移（stateless 重算）。"""
    _run(storage.save_models(_NIUCODE_MODELS))
    _register_wb(_WB_MODELS)
    try:
        _run(storage.save_model_visibility({"wbcn/wb-old": "show"}))
        # 目录刷新：wb-old 消失、新增无档位 wb-older 与有档位 wb-newer
        _register_wb([
            {"name": "wb-older", "context_window": 64000},
            {"name": "wb-newer", "completion_options":
             {"thinkingLevels": [{"level": "high"}]}},
        ])
        vis = {m["id"]: m["visible"]
               for m in client.get("/v1/models?all=1", headers=_auth()).json()["data"]}
        assert vis == {"牛码/glm-5.3": True, "wbcn/wb-older": False,
                       "wbcn/wb-newer": True}
        # 覆盖键持久保留：wb-old 重新上线时恢复用户意图（§5 已知边界）
        assert _run(storage.get_model_visibility()) == {"wbcn/wb-old": "show"}
    finally:
        _restore_wb()


# ─────────────────────────── chat 门控（联动） ───────────────────────────

def test_chat_hidden_model_404(client):
    """隐藏模型直调 → 404「未知或未启用的模型」（联动停用，与白名单同点位同文案）。"""
    _register_wb(_WB_MODELS)
    try:
        r = _post_chat(client, "wbcn/wb-old")
        assert r.status_code == 404
        assert "未知或未启用" in r.json()["detail"]
    finally:
        _restore_wb()


def test_chat_probe_exempt_from_visibility(client):
    """probe 豁免可见性（连通性测试需真到上游）：弹窗「先测后显示」不被门控锁死。"""
    _register_wb(_WB_MODELS)
    try:
        r = _post_chat(client, "wbcn/wb-old", probe=True)
        assert r.status_code == 200
        assert r.json()["choices"][0]["message"]["content"] == "wb-ok"
    finally:
        _restore_wb()


def test_chat_visible_enabled_ok_visible_disabled_404(client):
    """显示+启用 → 可调；显示但白名单单独关（可见但停用）→ 404（启用维度保留）。"""
    _register_wb(_WB_MODELS)
    try:
        assert _post_chat(client, "wbcn/wb-new").status_code == 200
        _run(storage.set_model_whitelist(["__none__"]))  # 全部禁用
        r = _post_chat(client, "wbcn/wb-new")
        assert r.status_code == 404 and "未知或未启用" in r.json()["detail"]
    finally:
        _restore_wb()


def test_niucode_default_visible_even_without_levels(client, monkeypatch):
    """牛码不参与默认隐藏（无档位也可见可调）；覆盖 show/hide 对其仍生效（§4.2 公式）。"""
    _run(storage.save_models(_NIUCODE_MODELS))

    async def _no_sync():
        return []

    monkeypatch.setattr(routes_mod.auth_flow, "sync_models", _no_sync)
    m = _run(routes_mod._ensure_model("glm-5.3"))
    assert m["name"] == "glm-5.3"
    # 手动 hide → 隐藏（公式统一，与是否 WB 无关）
    _run(storage.save_model_visibility({"glm-5.3": "hide"}))
    with pytest.raises(HTTPException) as ei:
        _run(routes_mod._ensure_model(_model_ref("glm-5.3").canonical))
    assert ei.value.status_code == 404


def test_custom_provider_default_visible_and_override(client):
    """自定义供应商默认恒可见（无档位不触发默认隐藏）；覆盖 hide 仍可停用。"""
    _run(providers_custom.upsert_provider(
        {"name": "基元律动", "base_url": "http://127.0.0.1:9/v1",
         "enabled": True, "models": ["m1"], "api_key": ""}))
    m = _run(routes_mod._ensure_model("基元律动/m1"))
    assert m["name"] == "m1" and m["provider"] == "基元律动"
    _run(storage.save_model_visibility({"基元律动/m1": "hide"}))
    with pytest.raises(HTTPException) as ei:
        _run(routes_mod._ensure_model("基元律动/m1"))
    assert ei.value.status_code == 404
