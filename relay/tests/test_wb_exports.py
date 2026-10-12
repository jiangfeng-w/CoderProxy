"""WorkBuddy 账号导出单测：cockpit 格式 / 9router 只读重建 / 合并策略 / 降级片段。

keyless：不依赖真实账号池数据；9router 库用 tmp SQLite 构造（含 0.5.99 形状的
providerConnections/apiKeys/kv），官方导出对拍口径在真机探针里做（本文件锁字段级行为）。
"""
import json
import sqlite3
import time

import pytest

from relay.config import settings
from relay.platforms import store
from relay.platforms.workbuddy import exports
from relay.platforms.workbuddy.exports import (
    ExportAccountNotFound,
    ExportRequestError,
    NineRouterUnavailable,
)


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))


def _account(**overrides) -> dict:
    base = {
        "uid": "uid-a",
        "nickname": "账号甲",
        "email": "",
        "enterprise_id": "",
        "domain": "www.workbuddy.cn",
        "access_token": "tok-a",
        "refresh_token": "ref-a",
        "expires_at": 1794144709133,
        "status": "normal",
    }
    base.update(overrides)
    return base


def _jwt(sub: str, exp_offset_s: int = 3600) -> str:
    import base64

    payload = {"sub": sub, "exp": int(time.time()) + exp_offset_s}
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{seg}.c2ln"


# ── cockpit 兼容格式 ──


def test_cockpit_export_matches_real_field_shape():
    """对齐用户实物（workbuddy_accounts_*.json）：id=md5(uid)、时间戳单位、字段清单。"""
    import hashlib

    items = exports.build_cockpit_export([_account()], now_s=1800000000)
    [item] = items
    assert item["id"] == f"workbuddy_{hashlib.md5(b'uid-a').hexdigest()}"
    assert item["uid"] == "uid-a"
    assert item["nickname"] == "账号甲"
    # cockpit 实物里 email 字段是昵称兜底（其本机导入的降级口径）
    assert item["email"] == "账号甲"
    assert item["access_token"] == "tok-a"
    assert item["refresh_token"] == "ref-a"
    assert item["token_type"] == "Bearer"
    assert item["expires_at"] == 1794144709133  # 毫秒
    assert item["created_at"] == 1800000000  # 秒
    assert item["last_used"] == 1800000000
    assert item["status"] == "normal"
    # 账号池没有的快照字段不输出（cockpit 导入容忍缺失）
    for absent in ("quota_raw", "auth_raw", "profile_raw", "usage_raw", "dosage_notify_code"):
        assert absent not in item


def test_cockpit_export_real_uid_id_vector():
    """真实文件反推向量：uid=7b8c5312-… → id=workbuddy_484f40674aac7caef126c03f37ee76c2。"""
    uid = "7b8c5312-6758-4080-af64-05c556a14602"
    [item] = exports.build_cockpit_export([_account(uid=uid)])
    assert item["id"] == "workbuddy_484f40674aac7caef126c03f37ee76c2"


def test_cockpit_export_email_prefers_real_email_and_enterprise():
    [item] = exports.build_cockpit_export([
        _account(email="a@b.c", enterprise_id="ent-1")])
    assert item["email"] == "a@b.c"
    assert item["enterprise_id"] == "ent-1"


async def test_cockpit_export_roundtrip_through_import_parser():
    """导出文本喂回导入解析器 → 同 uid/同 token（备份/迁移闭环）。"""
    from relay.platforms.workbuddy.imports import parse_import_text

    await store.upsert_account("workbuddy", _account())
    content = (await exports.export_selected([], "cockpit"))["content"]
    [payload] = parse_import_text(content)
    assert payload["uid"] == "uid-a"
    assert payload["access_token"] == "tok-a"
    assert payload["refresh_token"] == "ref-a"
    assert payload["expires_at"] == 1794144709133


# ── 9router：只读重建 ──


def _make_9router_db(path, connections=None, api_keys=None, kv=None, settings_data=None,
                     with_access_cols=True):
    """构造 0.5.99 形状的 9router 库（表结构与列名对齐实测）。"""
    con = sqlite3.connect(path)
    con.executescript("""
        CREATE TABLE settings (id INTEGER PRIMARY KEY CHECK (id = 1), data TEXT NOT NULL);
        CREATE TABLE providerConnections (
            id TEXT PRIMARY KEY, provider TEXT NOT NULL, authType TEXT NOT NULL,
            name TEXT, email TEXT, priority INTEGER, isActive INTEGER DEFAULT 1,
            data TEXT NOT NULL, createdAt TEXT NOT NULL, updatedAt TEXT NOT NULL);
        CREATE TABLE providerNodes (id TEXT PRIMARY KEY, type TEXT, name TEXT,
            data TEXT NOT NULL, createdAt TEXT NOT NULL, updatedAt TEXT NOT NULL);
        CREATE TABLE proxyPools (id TEXT PRIMARY KEY, isActive INTEGER DEFAULT 1,
            testStatus TEXT, data TEXT NOT NULL, createdAt TEXT NOT NULL, updatedAt TEXT NOT NULL);
        CREATE TABLE combos (id TEXT PRIMARY KEY, name TEXT UNIQUE NOT NULL, kind TEXT,
            models TEXT NOT NULL, createdAt TEXT NOT NULL, updatedAt TEXT NOT NULL);
        CREATE TABLE kv (scope TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
            PRIMARY KEY (scope, key));
    """)
    access_cols = ", accessRestricted INTEGER DEFAULT 0, accessAllow TEXT" if with_access_cols else ""
    con.executescript(f"""
        CREATE TABLE apiKeys (id TEXT PRIMARY KEY, key TEXT UNIQUE NOT NULL, name TEXT,
            machineId TEXT, isActive INTEGER DEFAULT 1, createdAt TEXT NOT NULL{access_cols});
    """)
    con.execute("INSERT INTO settings(id, data) VALUES(1, ?)",
                [json.dumps(settings_data or {"requireLogin": True})])
    for conn in connections or []:
        row = dict(conn)
        data = row.pop("data", {})
        con.execute(
            "INSERT INTO providerConnections(id, provider, authType, name, email, priority,"
            " isActive, data, createdAt, updatedAt) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [row["id"], row["provider"], row.get("authType", "oauth"), row.get("name"),
             row.get("email"), row.get("priority"), int(row.get("isActive", True)),
             json.dumps(data), row.get("createdAt", "2026-10-01T00:00:00.000Z"),
             row.get("updatedAt", "2026-10-01T00:00:00.000Z")])
    for key in api_keys or []:
        if with_access_cols:
            con.execute("INSERT INTO apiKeys(id, key, name, machineId, isActive, createdAt,"
                        " accessRestricted, accessAllow) VALUES(?,?,?,?,?,?,?,?)",
                        [key["id"], key["key"], key.get("name"), key.get("machineId"),
                         int(key.get("isActive", True)), key.get("createdAt", "2026-01-01T00:00:00Z"),
                         int(key.get("restricted", False)), json.dumps(key.get("allow", []))])
        else:
            con.execute("INSERT INTO apiKeys(id, key, name, machineId, isActive, createdAt)"
                        " VALUES(?,?,?,?,?,?)",
                        [key["id"], key["key"], key.get("name"), key.get("machineId"),
                         int(key.get("isActive", True)), key.get("createdAt", "2026-01-01T00:00:00Z")])
    for (scope, key, value) in kv or []:
        con.execute("INSERT INTO kv(scope, key, value) VALUES(?,?,?)",
                    [scope, key, json.dumps(value)])
    con.commit()
    con.close()


def _conn_row(conn_id="conn-1", provider="codebuddy-cn", token=None, priority=1, **data_extra):
    data = {
        "accessToken": token or _jwt("uid-a"),
        "refreshToken": "rt-1",
        "expiresAt": "2026-11-17T06:42:05.117Z",
        "expiresIn": 4751983,
        "testStatus": "active",
        "errorCode": None,
        "backoffLevel": 0,
        **data_extra,
    }
    return {"id": conn_id, "provider": provider, "authType": "oauth", "name": "conn",
            "email": None, "priority": priority, "isActive": True, "data": data}


def _flat_conn(conn_id="conn-1", provider="codebuddy-cn", token=None, priority=1, **data_extra):
    """合并层输入形态：扁平连接（= load_nine_router_backup 重建后的导出形态）。"""
    row = _conn_row(conn_id, provider, token, priority, **data_extra)
    data = row.pop("data")
    return {
        **data, **row,
        "createdAt": "2026-10-01T00:00:00.000Z",
        "updatedAt": "2026-10-01T00:00:00.000Z",
    }


def test_load_backup_reconstructs_all_sections(tmp_path, monkeypatch):
    db = tmp_path / "data.sqlite"
    _make_9router_db(db,
                     connections=[_conn_row()],
                     api_keys=[{"id": "k1", "key": "sk-x", "name": "Default",
                                "machineId": "m1", "restricted": True, "allow": ["gpt"]}],
                     kv=[("modelAliases", "alias-1", {"target": "glm"}),
                         ("customModels", "p|auto|llm", {"providerAlias": "p", "id": "auto"}),
                         ("mitmAlias", "claude", {"a": "b"}),
                         ("pricing", "codebuddy-cn", {"glm": 1})],
                     settings_data={"requireLogin": True, "providerThinking": {"codebuddy-cn": {"mode": "xhigh"}}})
    monkeypatch.setenv("RELAY_9ROUTER_DB", str(db))
    backup = exports.load_nine_router_backup()
    assert backup["settings"]["providerThinking"] == {"codebuddy-cn": {"mode": "xhigh"}}
    [conn] = backup["providerConnections"]
    assert conn["id"] == "conn-1" and conn["provider"] == "codebuddy-cn"
    assert conn["accessToken"] and conn["testStatus"] == "active"
    [key] = backup["apiKeys"]
    assert key["access"] == {"restricted": True, "allow": ["gpt"]}
    assert backup["modelAliases"] == {"alias-1": {"target": "glm"}}
    assert backup["customModels"][0]["id"] == "auto"
    assert backup["mitmAlias"] == {"claude": {"a": "b"}}
    assert backup["pricing"] == {"codebuddy-cn": {"glm": 1}}


def test_load_backup_old_schema_without_access_cols(tmp_path, monkeypatch):
    """旧版 apiKeys 无 access 两列 → 不输出 access 字段（版本自适应）。"""
    db = tmp_path / "data.sqlite"
    _make_9router_db(db, api_keys=[{"id": "k1", "key": "sk-x"}], with_access_cols=False)
    monkeypatch.setenv("RELAY_9ROUTER_DB", str(db))
    backup = exports.load_nine_router_backup()
    assert "access" not in backup["apiKeys"][0]


def test_load_backup_read_only_no_write(tmp_path, monkeypatch):
    """只读属性：重建后库文件内容/修改时间不变（mode=ro 不写）。"""
    db = tmp_path / "data.sqlite"
    _make_9router_db(db, connections=[_conn_row()])
    before = db.read_bytes()
    mtime_before = db.stat().st_mtime_ns
    monkeypatch.setenv("RELAY_9ROUTER_DB", str(db))
    exports.load_nine_router_backup()
    assert db.read_bytes() == before
    assert db.stat().st_mtime_ns == mtime_before


def test_load_backup_missing_db_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_9ROUTER_DB", str(tmp_path / "nope.sqlite"))
    with pytest.raises(NineRouterUnavailable):
        exports.load_nine_router_backup()


# ── 9router：合并策略（拍板①） ──


def _base_backup(connections):
    return {
        "settings": {"requireLogin": True},
        "providerConnections": connections,
        "providerNodes": [], "proxyPools": [],
        "apiKeys": [{"id": "k1", "key": "sk-x"}],
        "combos": [], "modelAliases": {"a": {"t": 1}}, "customModels": [],
        "mitmAlias": {"m": {"x": "y"}}, "pricing": {"p": 1},
    }


def test_merge_updates_matching_connection_and_clears_stale_errors():
    old_token = _jwt("sub-match")
    conn = _flat_conn("conn-hit", token=old_token, priority=3,
                      errorCode=1001, lastError="boom", backoffLevel=2,
                      **{"modelLock_glm-5.3": {"until": 1}, "rateLimitedUntil": "2026-10-12T00:00:00Z"})
    backup = _base_backup([conn])
    account = _account(uid="uid-differs", access_token=_jwt("sub-match"),
                       nickname="新昵称", refresh_token="rt-new", expires_at=1794144709133)
    merged = exports.merge_selected(backup, [account])
    [out] = merged["providerConnections"]
    # 凭证组更新
    assert out["refreshToken"] == "rt-new"
    assert out["expiresIn"] > 0
    # 瞬态错误态清除（防新凭证被旧判定误伤）
    assert out["testStatus"] == "active" and out["errorCode"] is None
    assert out["backoffLevel"] == 0 and out["lastError"] is None
    assert out["lastErrorAt"] is None and out["rateLimitedUntil"] is None
    assert out["modelLock_glm-5.3"] is None
    # 连接本体字段不动
    assert out["id"] == "conn-hit" and out["priority"] == 3
    assert out["createdAt"] == "2026-10-01T00:00:00.000Z"
    assert out["updatedAt"] != "2026-10-01T00:00:00.000Z"
    # 输入不被污染（深拷贝）
    assert backup["providerConnections"][0]["backoffLevel"] == 2


def test_merge_appends_new_and_preserves_untouched_bytewise():
    """拍板①：未选中/未命中连接逐字段原样（含其它平台）；新账号 append。"""
    other = _flat_conn("conn-other", provider="codex", token=_jwt("sub-other"))
    unmatched = _flat_conn("conn-keep", token=_jwt("sub-keep"), priority=7)
    backup = _base_backup([other, unmatched])
    merged = exports.merge_selected(backup, [_account(uid="uid-new", access_token=_jwt("sub-new"))])
    conns = merged["providerConnections"]
    assert len(conns) == 3
    assert conns[0] == other and conns[1] == unmatched  # 逐对象原样（未触任何字段）
    [new] = [c for c in conns if c["id"] not in ("conn-other", "conn-keep")]
    assert new["provider"] == "codebuddy-cn" and new["authType"] == "oauth"
    assert new["priority"] == 8  # max(1,7)+1
    assert new["accessToken"] == _jwt("sub-new")
    assert new["name"] == "账号甲"
    # 顶层其它段原样
    assert merged["settings"] == backup["settings"]
    assert merged["apiKeys"] == backup["apiKeys"]
    assert merged["pricing"] == backup["pricing"]


def test_merge_matches_by_pool_uid_when_token_not_jwt():
    """token 非 JWT 时用池内 uid 匹配（连接侧 sub=uid、账号侧 uid 同键）。"""
    conn2 = _flat_conn("c2", token=_jwt("uid-plain"))
    merged = exports.merge_selected(_base_backup([conn2]),
                                    [_account(uid="uid-plain", access_token="opaque-token")])
    [hit] = merged["providerConnections"]
    assert hit["accessToken"] == "opaque-token"  # 命中更新（uid 键匹配 sub）
    assert hit["id"] == "c2"


def test_new_connection_priority_starts_from_pool_max():
    merged = exports.merge_selected(_base_backup([]),
                                    [_account(uid="u1", access_token=_jwt("s1")),
                                     _account(uid="u2", access_token=_jwt("s2"))])
    assert [c["priority"] for c in merged["providerConnections"]] == [1, 2]


# ── 编排与降级 ──


async def test_export_selected_cockpit_all_and_subset():
    await store.upsert_account("workbuddy", _account(uid="u1"))
    await store.upsert_account("workbuddy", _account(uid="u2", access_token="t2"))
    all_res = await exports.export_selected([], "cockpit")
    assert all_res["mode"] == "cockpit" and all_res["format"] == "cockpit"
    assert len(json.loads(all_res["content"])) == 2
    one = await exports.export_selected(["u2"], "cockpit")
    [item] = json.loads(one["content"])
    assert item["uid"] == "u2"


async def test_export_selected_9router_full_backup(tmp_path, monkeypatch):
    db = tmp_path / "data.sqlite"
    _make_9router_db(db, connections=[_conn_row(token=_jwt("uid-a"))])
    monkeypatch.setenv("RELAY_9ROUTER_DB", str(db))
    await store.upsert_account("workbuddy", _account(access_token=_jwt("uid-a")))
    res = await exports.export_selected([], "9router")
    assert res["mode"] == "full_backup"
    doc = json.loads(res["content"])
    assert doc["settings"] == {"requireLogin": True}  # 配置随行
    assert doc["apiKeys"] == []  # 原库没有 → 原样
    [conn] = doc["providerConnections"]
    assert conn["refreshToken"] == "ref-a"  # 合并命中并更新
    assert res["filename"].startswith("9router-backup-merged-")


async def test_export_selected_9router_fragment_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("RELAY_9ROUTER_DB", str(tmp_path / "nope.sqlite"))
    await store.upsert_account("workbuddy", _account())
    res = await exports.export_selected([], "9router")
    assert res["mode"] == "fragment"
    [conn] = json.loads(res["content"])
    assert conn["provider"] == "codebuddy-cn"
    assert conn["accessToken"] == "tok-a"
    assert "整库覆盖" in res["note"]


async def test_export_selected_errors():
    await store.upsert_account("workbuddy", _account())
    with pytest.raises(ExportRequestError):
        await exports.export_selected([], "sub2api")
    with pytest.raises(ExportAccountNotFound):
        await exports.export_selected(["nope"], "cockpit")


async def test_export_selected_empty_pool():
    with pytest.raises(ExportRequestError, match="没有可导出"):
        await exports.export_selected([], "cockpit")
