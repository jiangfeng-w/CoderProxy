"""M8 Token 统计页 keyless 单测：/v1/stats 聚合查询 + API。

- 存储层：构造已知 logs，按 model / day / hour / kind 聚合，断言 digits 与日志明细一致。
- API：鉴权拦截、group_by 必填/合法性、透传过滤与 total 汇总正确。
不触发牛码网络；db 全部落 tmp 目录（monkeypatch settings.data_dir）。
"""
import asyncio

import pytest
from fastapi.testclient import TestClient

from relay import db
from relay.config import settings
from relay.routes import app as relay_app


def _run(coro):
    return asyncio.run(coro)


def _auth():
    return {"Authorization": "Bearer m8-test-key"}


# 已知数据（ts 全部显式 UTC ISO，保证 day/hour 聚合可断言）
def _seed(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(db.ensure_initialized())
    rows = [
        # (ts, kind, model, prompt, completion, cached, reasoning, total, stream)
        ("2026-09-04T00:00:00.000+00:00", "chat_done", "glm-x",
         10, 20, 5, 7, 30, 1),
        ("2026-09-04T02:10:00.000+00:00", "chat_error", "kimi-y",
         1, 2, 0, 0, 3, 0),
        ("2026-09-04T04:00:00.000+00:00", "chat_done", "glm-x",
         5, 5, 0, 0, 10, 1),
        ("2026-09-05T01:00:00.000+00:00", "chat_request", "glm-x",
         0, 0, 0, 0, 0, 1),
        ("2026-09-05T03:00:00.000+00:00", "chat_done", "glm-x",
         8, 9, 0, 0, 17, 0),
    ]
    for ts, kind, model, p, c, ca, r, t, s in rows:
        _run(db.log_event(kind=kind, model=model, ts=ts, stream=s,
                          prompt_tokens=p, completion_tokens=c,
                          cached_tokens=ca, reasoning_tokens=r,
                          total_tokens=t))


# ─────────────────────────── 存储层：各维度聚合 ───────────────────────────

def test_stats_by_day(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    rows, total = _run(db.query_stats(group_by="day"))
    by_key = {r["key"]: r for r in rows}
    assert set(by_key) == {"2026-09-04", "2026-09-05"}
    d4 = by_key["2026-09-04"]
    assert d4["requests"] == 3 and d4["success"] == 2 and d4["failed"] == 1
    assert d4["total_tokens"] == 43 and d4["prompt_tokens"] == 16
    d5 = by_key["2026-09-05"]
    assert d5["requests"] == 2 and d5["success"] == 1 and d5["failed"] == 0
    assert d5["total_tokens"] == 17
    # total：跨日未分组汇总
    assert total["requests"] == 5 and total["success"] == 3 and total["failed"] == 1
    assert total["prompt_tokens"] == 24 and total["completion_tokens"] == 36
    assert total["cached_tokens"] == 5 and total["reasoning_tokens"] == 7
    assert total["total_tokens"] == 60


def test_stats_by_hour(tmp_path, monkeypatch):
    """按小时聚合：key 为本地时区小时（依赖机器时区，故不硬编码字符串）。

    断言改为按「时刻升序 → key 升序」的位置校验数值（顺序与各 ts 的时刻固定一致），
    仅校验行数/唯一/升序与每行的聚合值，避免把机器时区写死进测试。
    """
    _seed(tmp_path, monkeypatch)
    rows, _ = _run(db.query_stats(group_by="hour"))
    assert len(rows) == 5
    keys = [r["key"] for r in rows]
    assert len(set(keys)) == 5
    assert keys == sorted(keys)
    # 按 ts 时刻升序对应的数值（ts 依次 = 上面 _seed 的行序）
    expected = [
        {"requests": 1, "success": 1, "failed": 0, "total_tokens": 30},
        {"requests": 1, "success": 0, "failed": 1, "total_tokens": 3},
        {"requests": 1, "success": 1, "failed": 0, "total_tokens": 10},
        {"requests": 1, "success": 0, "failed": 0, "total_tokens": 0},
        {"requests": 1, "success": 1, "failed": 0, "total_tokens": 17},
    ]
    for r, exp in zip(rows, expected):
        assert r["requests"] == exp["requests"]
        assert r["success"] == exp["success"] and r["failed"] == exp["failed"]
        assert r["total_tokens"] == exp["total_tokens"]


def test_stats_by_model(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    rows, _ = _run(db.query_stats(group_by="model"))
    by_key = {r["key"]: r for r in rows}
    assert by_key["glm-x"]["requests"] == 4
    assert by_key["glm-x"]["success"] == 3 and by_key["glm-x"]["failed"] == 0
    assert by_key["glm-x"]["total_tokens"] == 57
    assert by_key["kimi-y"]["requests"] == 1
    assert by_key["kimi-y"]["failed"] == 1 and by_key["kimi-y"]["success"] == 0
    assert by_key["kimi-y"]["total_tokens"] == 3


def test_stats_by_kind(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    rows, total = _run(db.query_stats(group_by="kind"))
    by_key = {r["key"]: r for r in rows}
    assert by_key["chat_done"]["requests"] == 3 and by_key["chat_done"]["success"] == 3
    assert by_key["chat_done"]["total_tokens"] == 57
    assert by_key["chat_error"]["requests"] == 1 and by_key["chat_error"]["failed"] == 1
    assert by_key["chat_request"]["requests"] == 1 and by_key["chat_request"]["failed"] == 0
    assert total["requests"] == 5


def test_stats_filters(tmp_path, monkeypatch):
    _seed(tmp_path, monkeypatch)
    # 过滤到单个模型
    rows, total = _run(db.query_stats(group_by="day", model="kimi-y"))
    assert total["requests"] == 1 and total["failed"] == 1
    assert len(rows) == 1 and rows[0]["key"] == "2026-09-04"
    # 过滤时间区间
    rows, total = _run(db.query_stats(
        group_by="day",
        time_from="2026-09-05T00:00:00.000+00:00",
        time_to="2026-09-05T23:59:59.000+00:00"))
    assert total["requests"] == 2 and total["success"] == 1
    assert [r["key"] for r in rows] == ["2026-09-05"]
    # 过滤类型（只统计 chat_error）
    rows, total = _run(db.query_stats(group_by="model", kind="chat_error"))
    assert total["requests"] == 1 and total["failed"] == 1
    assert [r["key"] for r in rows] == ["kimi-y"]


def test_stats_invalid_group_by(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    with pytest.raises(ValueError):
        _run(db.query_stats(group_by="bogus"))


def test_stats_empty_db(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    _run(db.ensure_initialized())
    rows, total = _run(db.query_stats(group_by="day"))
    assert rows == []
    assert total["requests"] == 0 and total["total_tokens"] == 0


# ─────────────────────────── /v1/stats API（验收 1/2）───────────────────────────

@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(settings, "relay_log_max_rows", 100000)
    settings.relay_api_key = "m8-test-key"
    with TestClient(relay_app, raise_server_exceptions=False) as c:
        yield c
    settings.relay_api_key = ""


def test_stats_endpoint_requires_auth_and_group_by(client):
    assert client.get("/v1/stats").status_code == 401
    assert client.get("/v1/stats", headers=_auth()).status_code == 422  # group_by 必填
    r = client.get("/v1/stats", params={"group_by": "bogus"}, headers=_auth())
    assert r.status_code == 400
    assert "group_by" in r.json()["detail"]


def test_stats_endpoint_aggregation(client):
    _run(db.log_event(kind="chat_done", model="glm-x", stream=1,
                      ts="2026-09-04T00:00:00.000+00:00",
                      prompt_tokens=10, completion_tokens=20, cached_tokens=5,
                      reasoning_tokens=7, total_tokens=30))
    _run(db.log_event(kind="chat_error", model="kimi-y", stream=0,
                      ts="2026-09-04T02:10:00.000+00:00",
                      prompt_tokens=1, completion_tokens=2, total_tokens=3))
    _run(db.log_event(kind="chat_done", model="glm-x", stream=1,
                      ts="2026-09-05T01:00:00.000+00:00",
                      prompt_tokens=8, completion_tokens=9, total_tokens=17))

    r = client.get("/v1/stats", params={"group_by": "day"}, headers=_auth())
    assert r.status_code == 200
    body = r.json()
    by_key = {row["key"]: row for row in body["rows"]}
    assert set(by_key) == {"2026-09-04", "2026-09-05"}
    assert by_key["2026-09-04"]["requests"] == 2
    assert by_key["2026-09-04"]["success"] == 1 and by_key["2026-09-04"]["failed"] == 1
    assert by_key["2026-09-05"]["total_tokens"] == 17
    assert body["total"]["requests"] == 3
    assert body["total"]["total_tokens"] == 50

    r = client.get("/v1/stats", params={"group_by": "model", "model": "glm-x"},
                   headers=_auth())
    body = r.json()
    assert body["total"]["requests"] == 2 and body["total"]["failed"] == 0
    assert [row["key"] for row in body["rows"]] == ["glm-x"]
    # 无数据 → 空 rows 与全 0 total
    r = client.get("/v1/stats", params={"group_by": "day",
                                        "time_from": "2030-01-01T00:00:00.000+00:00"},
                   headers=_auth())
    body = r.json()
    assert body["rows"] == [] and body["total"]["requests"] == 0
