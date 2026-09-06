"""M6 数据底座 keyless 单测：SQLite 日志存储层 + /v1/logs API + 流式 usage 落库。

不触发牛码网络请求：
- db 落 tmp 目录（monkeypatch settings.data_dir）；
- chat 端点走 fake provider / monkeypatch routes 内部转发函数；
- 流式 usage 走 oai_adapter.UsageCollector + 假 provider 事件。
"""
import asyncio
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.models.schemas import ChatMessage, ChatRequest, ChatResponse, Usage

from relay import db, oai_adapter, routes as routes_mod, storage
from relay.config import settings
from relay.routes import app as relay_app


def _run(coro):
    return asyncio.run(coro)


def _auth():
    return {"Authorization": "Bearer m6-test-key"}


# ─────────────────────────── 存储层：建库 / 插入 / 查询 ───────────────────────────

def test_db_create_insert_query(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(db.ensure_initialized())
    assert db.db_path().exists()
    rid = _run(db.log_event(kind="chat_request", model="glm-x", stream=1,
                            detail={"tools": 3}))
    assert rid >= 1
    _run(db.log_event(kind="chat_done", model="glm-x", stream=1, prompt_tokens=10,
                      completion_tokens=20, cached_tokens=5, reasoning_tokens=7,
                      total_tokens=30, duration_ms=123))
    rows, total = _run(db.query_logs())
    assert total == 2
    # id 倒序：最新在前
    assert rows[0]["kind"] == "chat_done"
    assert rows[0]["prompt_tokens"] == 10
    assert rows[0]["cached_tokens"] == 5
    assert rows[0]["reasoning_tokens"] == 7
    assert rows[0]["duration_ms"] == 123
    assert rows[1]["kind"] == "chat_request"
    assert rows[1]["stream"] == 1
    assert rows[1]["detail"] == {"tools": 3}  # detail JSON 往返


def test_db_persists_to_file(tmp_path, monkeypatch):
    """验收 2：落 SQLite 非内存——跨“进程”直接读盘仍有数据。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(db.ensure_initialized())
    _run(db.log_event(kind="chat_done", model="m", stream=0))
    conn = sqlite3.connect(str(db.db_path()))  # 新连接直读，绕过模块缓存
    try:
        n = conn.execute("SELECT COUNT(*) FROM logs WHERE kind='chat_done'").fetchone()[0]
    finally:
        conn.close()
    assert n == 1


def test_db_query_requires_columns_present(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(db.ensure_initialized())
    row_id = _run(db.log_event(kind="chat_done", model="x", stream=0,
                               prompt_tokens=1, completion_tokens=2,
                               cached_tokens=3, reasoning_tokens=4,
                               total_tokens=6, duration_ms=9))
    rows, _ = _run(db.query_logs())
    assert rows[0]["id"] == row_id
    assert rows[0]["ts"]  # ts 自动落 UTC ISO（含毫秒）


# ─────────────────────────── 存储层：分页 / 筛选 ───────────────────────────

def test_db_pagination(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    for i in range(5):
        _run(db.log_event(kind="chat_request", model=f"m{i}", stream=0))
    rows, total = _run(db.query_logs(limit=2, offset=0))
    assert total == 5
    assert [r["model"] for r in rows] == ["m4", "m3"]
    rows2, _ = _run(db.query_logs(limit=2, offset=2))
    assert [r["model"] for r in rows2] == ["m2", "m1"]


def test_db_filter_kind_model_time_combine(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(db.log_event(kind="chat_request", model="m1", stream=1,
                      ts="2026-09-04T00:00:00.000+00:00"))
    _run(db.log_event(kind="chat_done", model="m1", stream=1,
                      ts="2026-09-04T01:00:00.000+00:00"))
    _run(db.log_event(kind="chat_done", model="m2", stream=0,
                      ts="2026-09-04T02:00:00.000+00:00"))

    rows, total = _run(db.query_logs(kind="chat_request"))
    assert total == 1 and rows[0]["model"] == "m1"

    rows, total = _run(db.query_logs(model="m2"))
    assert total == 1 and rows[0]["kind"] == "chat_done"

    rows, total = _run(db.query_logs(
        time_from="2026-09-04T00:30:00.000+00:00",
        time_to="2026-09-04T01:30:00.000+00:00"))
    assert total == 1 and rows[0]["model"] == "m1" and rows[0]["kind"] == "chat_done"

    rows, total = _run(db.query_logs(
        kind="chat_done", model="m1",
        time_from="2026-09-04T00:00:00.000+00:00",
        time_to="2026-09-04T03:00:00.000+00:00"))
    assert total == 1


def test_db_distinct_kinds_models(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(db.log_event(kind="chat_request", model="m1"))
    _run(db.log_event(kind="chat_done", model="m1"))
    _run(db.log_event(kind="chat_error", model="m2"))
    _run(db.log_event(kind="chat_done", model="m2"))
    assert set(_run(db.distinct_kinds())) == {"chat_request", "chat_done", "chat_error"}
    assert set(_run(db.distinct_models())) == {"m1", "m2"}


def test_db_clear_by_condition_and_all(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(db.log_event(kind="chat_request", model="m1"))
    _run(db.log_event(kind="chat_done", model="m1"))
    _run(db.log_event(kind="chat_error", model="m2"))
    # 按条件清空
    deleted = _run(db.clear_logs(kind="chat_request"))
    assert deleted == 1
    _, total = _run(db.query_logs())
    assert total == 2
    # 全清
    deleted = _run(db.clear_logs())
    assert deleted == 2
    _, total = _run(db.query_logs())
    assert total == 0


def test_db_prune_removes_oldest(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    for i in range(5):
        _run(db.log_event(kind="chat_request", model=f"m{i}", stream=0))
    deleted = _run(db.prune(max_rows=3))
    assert deleted == 2
    rows, total = _run(db.query_logs())
    assert total == 3
    # 最旧两条（m0/m1）被滚动删除；desc 首条是 m4
    assert rows[0]["model"] == "m4"
    # 不超上限 → 不删
    assert _run(db.prune(max_rows=3)) == 0


# ─────────────────────────── 存储层：schema 自省补列（验收 10）───────────────────────────

def test_db_schema_auto_add_column(tmp_path, monkeypatch):
    """旧库缺列 → 启动补列；旧行该列为 NULL；新写入正常。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    p = db.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                 "ts TEXT NOT NULL, kind TEXT NOT NULL, model TEXT)")
    conn.execute("INSERT INTO logs (ts, kind, model) "
                 "VALUES ('2026-09-04T00:00:00.000+00:00', 'chat_request', 'old')")
    conn.commit()
    conn.close()

    _run(db.ensure_initialized())
    cols = {r[1] for r in sqlite3.connect(str(p)).execute("PRAGMA table_info(logs)")}
    assert {"stream", "prompt_tokens", "cached_tokens", "duration_ms", "detail"} <= cols
    rows, total = _run(db.query_logs())
    assert total == 1
    assert rows[0]["model"] == "old"
    assert rows[0]["prompt_tokens"] is None and rows[0]["detail"] is None
    # 补列后新写入正常
    _run(db.log_event(kind="chat_done", model="new", prompt_tokens=9))
    rows, total = _run(db.query_logs())
    assert total == 2
    assert rows[0]["prompt_tokens"] == 9


# ─────────────────────────── /v1/logs API（验收 3/4/6）───────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "relay_log_max_rows", 100000)
    settings.relay_api_key = "m6-test-key"

    # keyless 单测基准：模拟「已登录 + 对 agent 的 /v1 服务开启」，聚焦后端逻辑本身
    async def _serving_on():
        return True

    monkeypatch.setattr(routes_mod, "_serving", _serving_on)

    # raise_server_exceptions=False：chat_error 路径返回 500 而非向测试抛异常
    with TestClient(relay_app, raise_server_exceptions=False) as c:
        yield c
    settings.relay_api_key = ""


def test_logs_endpoints_require_auth(client):
    assert client.get("/v1/logs").status_code == 401
    assert client.get("/v1/logs/kinds").status_code == 401
    assert client.get("/v1/logs/models").status_code == 401
    assert client.delete("/v1/logs").status_code == 401


def test_logs_list_endpoint(client):
    _run(db.log_event(kind="chat_request", model="glm-x", stream=1, detail={"tools": 2}))
    _run(db.log_event(kind="chat_done", model="glm-x", stream=1, prompt_tokens=3,
                      completion_tokens=4, total_tokens=7))
    _run(db.log_event(kind="chat_error", model="kimi-y", stream=0,
                      detail={"error": "boom"}))
    r = client.get("/v1/logs", headers=_auth())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 3
    assert len(body["rows"]) == 3
    assert body["rows"][0]["kind"] == "chat_error"  # 最新在前
    assert body["rows"][2]["detail"] == {"tools": 2}
    assert set(body["kinds"]) == {"chat_request", "chat_done", "chat_error"}
    assert set(body["models"]) == {"glm-x", "kimi-y"}
    assert all("usage" not in row for row in body["rows"])


def test_logs_list_pagination_and_filter_endpoint(client):
    for i in range(5):
        _run(db.log_event(kind="chat_request", model=f"m{i}", stream=0))
    r = client.get("/v1/logs", params={"limit": 2, "offset": 0}, headers=_auth())
    body = r.json()
    assert body["total"] == 5 and len(body["rows"]) == 2
    r = client.get("/v1/logs", params={"limit": 2, "offset": 2}, headers=_auth())
    assert [row["model"] for row in r.json()["rows"]] == ["m2", "m1"]
    r = client.get("/v1/logs", params={"model": "m3"}, headers=_auth())
    assert r.json()["total"] == 1
    # limit 上限 200
    r = client.get("/v1/logs", params={"limit": 9999}, headers=_auth())
    assert len(r.json()["rows"]) == 5


def test_logs_kinds_models_endpoints(client):
    _run(db.log_event(kind="chat_done", model="glm-x"))
    assert client.get("/v1/logs/kinds", headers=_auth()).json()["kinds"] == ["chat_done"]
    assert client.get("/v1/logs/models", headers=_auth()).json()["models"] == ["glm-x"]


def test_logs_delete_endpoint(client):
    _run(db.log_event(kind="chat_request", model="m1"))
    _run(db.log_event(kind="chat_done", model="m1"))
    r = client.delete("/v1/logs", params={"kind": "chat_request"}, headers=_auth())
    assert r.status_code == 200
    assert r.json()["deleted"] == 1
    _, total = _run(db.query_logs())
    assert total == 1
    r = client.delete("/v1/logs", headers=_auth())
    assert r.json()["deleted"] == 1
    _, total = _run(db.query_logs())
    assert total == 0


# ─────────────────────────── chat 落库：非流式（含 usage/duration）───────────────────────────

def test_chat_non_stream_logs_request_done(client, monkeypatch):
    async def _fake_chat(model_name, chat_request, ctx, *, probe=False):
        return ChatResponse(content="ok", finish_reason="stop",
                            usage=Usage(prompt_tokens=11, completion_tokens=22,
                                        total_tokens=33, cached_input_tokens=4,
                                        reasoning_tokens=5))

    monkeypatch.setattr(routes_mod, "_chat_with_retry", _fake_chat)
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-x",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth())
    assert resp.status_code == 200
    # chat_request：tools + 思考下发态走 detail（agent 未传思考参数 → 假关默认 none）
    reqs, _ = _run(db.query_logs(kind="chat_request"))
    assert len(reqs) == 1 and reqs[0]["model"] == "glm-x"
    assert reqs[0]["detail"] == {"tools": 0, "thinking": True, "thinking_effort": "none"}
    assert reqs[0]["stream"] == 0
    # chat_done：usage 各 token + duration
    dones, _ = _run(db.query_logs(kind="chat_done"))
    row = dones[0]
    assert row["prompt_tokens"] == 11 and row["completion_tokens"] == 22
    assert row["cached_tokens"] == 4 and row["reasoning_tokens"] == 5
    assert row["total_tokens"] == 33 and row["stream"] == 0
    assert row["duration_ms"] is not None and row["duration_ms"] >= 0


def test_chat_error_logged(client, monkeypatch):
    async def _boom(model_name, chat_request, ctx, *, probe=False):
        raise RuntimeError("boom upstream")

    monkeypatch.setattr(routes_mod, "_chat_with_retry", _boom)
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-x",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth())
    assert resp.status_code == 500
    rows, _ = _run(db.query_logs(kind="chat_error"))
    assert len(rows) == 1
    assert "boom" in rows[0]["detail"]["error"]
    assert rows[0]["stream"] == 0
    assert len(rows[0]["detail"]["error"]) <= 500  # 摘要截断


# ─────────────────────────── 流式 usage 落库（验收 11：include_usage=false 也能拿）───────────────────────────

class _FakeProvider:
    """仅实现 stream_structured，产出预置事件。"""

    def __init__(self, events):
        self._events = events
        self._model_name = "m"

    async def stream_structured(self, request):
        for e in self._events:
            yield e


@pytest.mark.asyncio
async def test_stream_collector_gets_usage_without_include_usage():
    """验收 11：不传 stream_options.include_usage 时，UsageCollector 仍拿到 usage。"""
    events = [
        {"type": "content", "delta": "hi"},
        {"type": "done", "content": "hi", "tool_calls": [], "finish_reason": "stop",
         "usage": Usage(prompt_tokens=5, completion_tokens=6, total_tokens=11,
                        cached_input_tokens=2, reasoning_tokens=1)},
    ]
    p = _FakeProvider(events)
    req = ChatRequest(model="m", messages=[ChatMessage(role="user", content="x")])
    col = oai_adapter.UsageCollector()
    frames = [f async for f in
              oai_adapter.stream_openai_sse(p, req, include_usage=False,
                                            usage_collector=col)]
    assert col.usage is not None
    assert col.usage.prompt_tokens == 5 and col.usage.cached_input_tokens == 2
    assert col.usage.reasoning_tokens == 1
    assert col.duration_ms is not None and col.duration_ms >= 0
    # include_usage=False → 无 usage SSE 帧
    for fr in frames:
        if fr.startswith("data: {"):
            payload = json.loads(fr[len("data: "):])
            assert "usage" not in payload


def test_usage_collector_reset_semantics():
    """401 重试语义：每次 attempt 前 reset，只保留最后一次 usage。"""
    col = oai_adapter.UsageCollector()
    col.record(Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2))
    assert col.usage is not None and col.duration_ms is not None
    col.reset()
    assert col.usage is None and col.duration_ms is None
    col.record(Usage(prompt_tokens=9, completion_tokens=9, total_tokens=18))
    assert col.usage.prompt_tokens == 9


def test_chat_stream_logs_done_usage(client, monkeypatch):
    """流式 chat_done 落库 usage；agent 不传 include_usage 也能落非全 0 token。"""
    # 前置白名单校验（SSE 建立前执行）需模型在目录且启用；mock 转发层不再覆盖该校验
    _run(storage.save_models(
        [{"name": "glm-x", "api_key": "k", "base_url": "b", "anthropic": False}]))
    _run(storage.set_model_whitelist(["glm-x"]))

    async def _fake_sse(model_name, chat_request, include_usage, ctx,
                        usage_collector=None, *, probe=False):
        assert usage_collector is not None
        usage_collector.record(Usage(prompt_tokens=1, completion_tokens=2,
                                     total_tokens=3, cached_input_tokens=0,
                                     reasoning_tokens=1))
        yield "data: {\"choices\":[{\"delta\":{\"content\":\"hi\"}}]}\n\n"
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(routes_mod, "_sse_with_retry", _fake_sse)
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-x", "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth())
    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text
    dones, _ = _run(db.query_logs(kind="chat_done"))
    row = dones[0]
    assert row["stream"] == 1
    assert row["prompt_tokens"] == 1 and row["completion_tokens"] == 2
    assert row["total_tokens"] == 3 and row["reasoning_tokens"] == 1
    assert row["duration_ms"] is not None


# ─────────────────────────── 401 重试：auth_401_refresh 落库 + 取重试 usage ───────────────────────────

def test_upstream_401_retry_logs_refresh_and_usage(client, monkeypatch):
    class _RetryProvider:
        def __init__(self):
            self.calls = 0

        async def chat(self, request):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("upstream 401 ...")
            return ChatResponse(content="ok",
                                usage=Usage(prompt_tokens=7, completion_tokens=8,
                                            total_tokens=15))

    async def _no_sync():
        return []

    async def _find_model(name):
        return {"name": name, "api_key": "k", "base_url": "b", "anthropic": False}

    retry = _RetryProvider()  # 共享同一实例：第 1 次 401、第 2 次成功
    monkeypatch.setattr(routes_mod, "build_provider",
                        lambda model, name, ctx: retry)
    monkeypatch.setattr(routes_mod.auth_flow, "sync_models", _no_sync)
    monkeypatch.setattr(storage, "find_model", _find_model)

    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-x",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=_auth())
    assert resp.status_code == 200
    refreshes, _ = _run(db.query_logs(kind="auth_401_refresh"))
    assert len(refreshes) == 1 and refreshes[0]["model"] == "glm-x"
    dones, _ = _run(db.query_logs(kind="chat_done"))
    # 401 重试成功后取重试那次 usage（非全 0）
    assert dones[0]["prompt_tokens"] == 7
    assert dones[0]["completion_tokens"] == 8
