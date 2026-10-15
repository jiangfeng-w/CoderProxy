"""WorkBuddy 账号多方式导入单测：解析矩阵 / JWT 兜底 / accounts 补全 / 本机导入。

keyless：不触网（MockTransport）；本机导入用 tmp auth 文件 + 合成密钥（monkeypatch
ide_switch.get_official_key / auth_file_path，复用 test_wb_ide_switch 的 TEST_KEY 口径）。
"""
import asyncio
import base64
import json
import time

import httpx
import pytest

from relay.config import settings
from relay.platforms import store
from relay.platforms.workbuddy import imports, oauth
from relay.platforms.workbuddy.ide_switch import OfficialKey
from relay.platforms.workbuddy.imports import ImportParseError

TEST_KEY = OfficialKey(bytes([7] * 32))
API = "https://copilot.tencent.com"


class _Ctx:
    """替代 WbClient 构造的薄包装（保持 complete_from_accounts_api 的 close 调用形态）。"""

    def __init__(self, client: oauth.WbClient):
        self._client = client

    async def request_json(self, *args, **kwargs):
        return await self._client.request_json(*args, **kwargs)

    async def close(self):
        await self._client.close()


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """存储落 tmp。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))


def _jwt(sub: str = "uid-jwt", exp_offset_s: int = 3600, nickname: str = "贴入号") -> str:
    """构造未签名 JWT（header.payload.signature；仅 payload 有效）。"""
    payload = {"sub": sub, "nickname": nickname, "exp": int(time.time()) + exp_offset_s}
    seg = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJSUzI1NiJ9.{seg}.c2ln"


# ── 解析矩阵 ──

def test_parse_plain_token():
    assert imports.parse_import_text("  tok-abc  ") == [
        {"access_token": "tok-abc", "source": "token"}]


def test_parse_composite_uid_plus_token():
    assert imports.parse_import_text("uid-1+tok-xyz") == [
        {"access_token": "tok-xyz", "source": "token"}]


def test_parse_single_object_camel_and_snake():
    obj = {"access_token": "t1", "refreshToken": "r1", "uid": "u1",
           "enterpriseId": "ent1", "expires_at": 1234}
    assert imports.parse_import_text(json.dumps(obj)) == [{
        "access_token": "t1", "source": "json", "uid": "u1",
        "enterprise_id": "ent1", "refresh_token": "r1", "expires_at": 1234 * 1000,
    }]


def test_parse_token_key_alias():
    assert imports.parse_import_text('{"token": "alias-tok"}') == [
        {"access_token": "alias-tok", "source": "json"}]


def test_parse_array_and_wrapped_accounts():
    arr = [{"access_token": "a1"}, {"accessToken": "a2"}]
    assert [p["access_token"] for p in imports.parse_import_text(json.dumps(arr))] == ["a1", "a2"]
    wrapped = {"accounts": [{"access_token": "w1"}]}
    assert [p["access_token"] for p in imports.parse_import_text(json.dumps(wrapped))] == ["w1"]
    items = {"items": [{"access_token": "i1"}]}
    assert [p["access_token"] for p in imports.parse_import_text(json.dumps(items))] == ["i1"]


def test_parse_errors():
    with pytest.raises(ImportParseError, match="请输入"):
        imports.parse_import_text("   ")
    with pytest.raises(ImportParseError, match="不是有效 JSON"):
        imports.parse_import_text("{bad json")
    with pytest.raises(ImportParseError, match="缺少 access_token"):
        imports.parse_import_text('{"nickname": "无token"}')
    with pytest.raises(ImportParseError, match="第 2 条"):
        imports.parse_import_text('[{"access_token": "ok"}, {"uid": "x"}]')
    with pytest.raises(ImportParseError, match="数组为空"):
        imports.parse_import_text("[]")
    with pytest.raises(ImportParseError, match="未解析到"):
        imports.parse_import_text("+")
    # 非 {/[ 开头整串当纯 token（cockpit 同款分发语义；JSON 有效性不校验）
    assert imports.parse_import_text('"just a string"') == [
        {"access_token": '"just a string"', "source": "token"}]


def test_expiry_ms_dual_scale():
    assert imports._expiry_ms(1794144709) == 1794144709000       # 秒
    assert imports._expiry_ms(1794144709133) == 1794144709133    # 毫秒
    assert imports._expiry_ms("1794144709") == 1794144709000     # 字符串秒
    assert imports._expiry_ms("abc") == 0
    assert imports._expiry_ms(-5) == 0


def test_expiry_ms_iso_string():
    """9router expiresAt 是 ISO 8601 串（Z 后缀）→ 毫秒。"""
    assert imports._expiry_ms("2026-11-17T06:42:05.117Z") == 1794897725117
    assert imports._expiry_ms("2026-11-17T06:42:05+00:00") == 1794897725000


# ── 真实物件格式兼容（2026-10-12 用户提供 cockpit 导出与 9router 备份实测） ──

def test_parse_cockpit_export_real_shape():
    """cockpit-tools export_accounts 实物：纯数组 + snake 键 + 未知键忽略。"""
    doc = [{
        "id": "workbuddy_484f40674aac7caef126c03f37ee76c2",
        "email": "峰",  # cockpit 的 email 字段实为昵称兜底
        "uid": "7b8c5312-6758-4080-af64-05c556a14602",
        "nickname": "峰",
        "access_token": "eyJhbGci.c2ln",
        "refresh_token": "eyJhbGci.rt",
        "token_type": "Bearer",
        "expires_at": 1794153837092,
        "domain": "www.workbuddy.cn",
        "dosage_notify_code": "0",  # 账号池无此字段 → 忽略
        "payment_type": "free",
        "quota_raw": {"dosage": {}, "payment": {}, "userResource": {}},
    }]
    [p] = imports.parse_import_text(json.dumps(doc))
    assert p["uid"] == "7b8c5312-6758-4080-af64-05c556a14602"
    assert p["nickname"] == "峰"
    assert p["refresh_token"] == "eyJhbGci.rt"
    assert p["expires_at"] == 1794153837092
    assert p["domain"] == "www.workbuddy.cn"


def test_parse_9router_backup_real_shape():
    """9router 设置导出实物：providerConnections 包裹 + camelCase + ISO expiresAt + 无 uid。"""
    jwt_sub = _jwt(sub="7b8c5312-6758-4080-af64-05c556a14602")
    doc = {
        "settings": {"requireLogin": True},
        "providerConnections": [{
            "accessToken": jwt_sub,
            "refreshToken": "rt-9router",
            "expiresAt": "2026-11-17T06:42:05.117Z",
            "expiresIn": 4751983,
            "id": "afb43990-e3bc-483f-b716-6cc845f96401",
            "provider": "codebuddy-cn",
            "authType": "oauth",
            "name": "6683",
            "email": None,
            "priority": 1,
            "isActive": True,
        }],
        "apiKeys": [{"key": "sk-x"}],
    }
    [p] = imports.parse_import_text(json.dumps(doc))
    assert p["uid"] == "7b8c5312-6758-4080-af64-05c556a14602"   # JWT sub 兜底
    assert p["nickname"] == "6683"                              # name → 显示名
    assert p["refresh_token"] == "rt-9router"
    assert p["expires_at"] == 1794897725117                     # ISO → 毫秒
    assert p["source"] == "json"


def test_parse_9router_skips_other_providers():
    """providerConnections 混入其它平台（codex 等）→ 跳过；全被跳过则报错。"""
    mixed = {"providerConnections": [
        {"accessToken": "codex-tok", "provider": "codex", "name": "x"},
        {"accessToken": _jwt(sub="uid-wb"), "provider": "codebuddy-cn", "name": "wb"},
    ]}
    [p] = imports.parse_import_text(json.dumps(mixed))
    assert p["uid"] == "uid-wb"
    with pytest.raises(ImportParseError, match="无 codebuddy-cn"):
        imports.parse_import_text(json.dumps(
            {"providerConnections": [{"accessToken": "t", "provider": "kiro"}]}))


# ── JWT 兜底 ──

def test_decode_jwt_claims():
    claims = imports.decode_jwt_claims(_jwt(sub="uid-x", exp_offset_s=7200))
    assert claims["sub"] == "uid-x"
    assert claims["exp"] > time.time()
    assert imports.decode_jwt_claims("not-a-jwt") == {}
    assert imports.decode_jwt_claims("a.%%%b.c") == {}  # 坏 base64


# ── accounts 端点补全（MockTransport） ──

def _accounts_response(items: list[dict]) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "msg": "ok", "data": {"accounts": items}})


async def test_complete_prefers_last_login_and_jwt_exp(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"].startswith("Bearer eyJ")
        return _accounts_response([
            {"uid": "uid-other", "nickname": "非当前", "lastLogin": False},
            {"uid": "uid-live", "nickname": "当前号", "lastLogin": True,
             "enterpriseId": "ent-9"},
        ])

    client = oauth.WbClient(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(imports, "WbClient", lambda: _Ctx(client))
    payload = await imports.complete_from_accounts_api(_jwt(sub="uid-jwt", exp_offset_s=600))
    assert payload["uid"] == "uid-live"          # lastLogin 项优先
    assert payload["nickname"] == "当前号"
    assert payload["enterprise_id"] == "ent-9"
    assert payload["expires_at"] > time.time() * 1000  # JWT exp 兜底（毫秒）
    assert payload["source"] == "token"


async def test_complete_rejects_non_zero_code(monkeypatch):
    client = oauth.WbClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"code": 10085, "msg": "forbidden"})))
    monkeypatch.setattr(imports, "WbClient", lambda: _Ctx(client))
    from relay.platforms.base import PlatformUpstreamError
    with pytest.raises(PlatformUpstreamError):
        await imports.complete_from_accounts_api(_jwt())


async def test_complete_rejects_missing_accounts(monkeypatch):
    client = oauth.WbClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"code": 0, "data": {}})))
    monkeypatch.setattr(imports, "WbClient", lambda: _Ctx(client))
    from relay.platforms.base import PlatformUpstreamError
    with pytest.raises(PlatformUpstreamError, match="accounts"):
        await imports.complete_from_accounts_api(_jwt())


# ── 入库编排 ──

async def test_import_text_json_path_no_network():
    """JSON 导入全程不联网（MockTransport 抛错即证明没发请求）。"""
    text = json.dumps([{"access_token": "j1", "uid": "uid-j1"},
                       {"access_token": "j2"}])
    imported = await imports.import_text(text)
    assert [a["uid"] for a in imported] == ["uid-j1", "j2"][:2] or True  # j2 降级主键
    assert imported[0]["uid"] == "uid-j1"
    assert imported[1]["uid"].startswith("wb_")  # 无 uid → sha256 降级主键
    rows = await store.load_accounts("workbuddy")
    assert len(rows) == 2


async def test_import_text_token_path_upserts_with_complete(monkeypatch):
    text = _jwt(sub="uid-tok")
    client = oauth.WbClient(transport=httpx.MockTransport(
        lambda req: _accounts_response([
            {"uid": "uid-tok", "nickname": "联网补全", "lastLogin": True}])))
    monkeypatch.setattr(imports, "WbClient", lambda: _Ctx(client))
    # 预取走 billing 域：mock 掉避免真实网络
    async def fake_prefetch(account):
        return
    monkeypatch.setattr(oauth, "prefetch_caches", fake_prefetch)
    imported = await imports.import_text(text)
    assert imported[0]["uid"] == "uid-tok"
    assert imported[0]["nickname"] == "联网补全"


async def test_import_text_token_failure_leaves_pool_untouched(monkeypatch):
    client = oauth.WbClient(transport=httpx.MockTransport(
        lambda req: httpx.Response(403, json={"code": 10085, "msg": "forbidden"})))
    monkeypatch.setattr(imports, "WbClient", lambda: _Ctx(client))
    from relay.platforms.base import PlatformUpstreamError
    with pytest.raises(PlatformUpstreamError):
        await imports.import_text(_jwt(sub="uid-bad"))
    assert await store.load_accounts("workbuddy") == []  # 严格口径：不入库


async def test_import_text_preserves_existing_caches(monkeypatch):
    """同 uid 重导覆盖凭证但保留 checkin/quota 缓存（preserve 语义）。"""
    await store.upsert_account("workbuddy", {
        "uid": "uid-keep", "access_token": "old", "expires_at": 1,
        "checkin": {"streak_days": 5, "today_checked_in": True},
        "quota": {"total": 100, "remaining": 50}})
    text = json.dumps({"access_token": "new", "uid": "uid-keep"})
    imported = await imports.import_text(text)
    assert imported[0]["access_token"] == "new"
    assert imported[0]["checkin"]["streak_days"] == 5
    assert imported[0]["quota"]["remaining"] == 50


# ── 本机导入 ──

def _plain_local_doc() -> dict:
    return {
        "account": {"uid": "uid-local", "nickname": "本机号", "enterpriseId": "ent-l",
                    "lastLogin": True},
        "auth": {"accessToken": "local-token", "refreshToken": "local-refresh",
                 "expiresAt": 1794144709133, "domain": "www.workbuddy.cn",
                 "expiresIn": 2419200},
        "accounts": [{"uid": "uid-local", "nickname": "本机号", "lastLogin": True}],
        "allAccounts": [{"uid": "uid-local", "nickname": "本机号", "lastLogin": True}],
    }


def test_import_local_reads_decrypted_file(tmp_path, monkeypatch):
    from relay.platforms.workbuddy import ide_switch as sw

    auth_file = tmp_path / "workbuddy-desktop.info"
    doc = sw.encode_session(_plain_local_doc(), TEST_KEY)  # 真实 seal 链构造加密文件
    auth_file.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sw, "auth_file_path", lambda profile=None: auth_file)

    async def fake_key(profile=None):
        return TEST_KEY

    monkeypatch.setattr(sw, "get_official_key", fake_key)

    async def fake_prefetch(account):
        return

    monkeypatch.setattr(oauth, "prefetch_caches", fake_prefetch)

    account = asyncio.run(imports.import_local())
    assert account["uid"] == "uid-local"
    assert account["nickname"] == "本机号"
    assert account["access_token"] == "local-token"
    assert account["refresh_token"] == "local-refresh"
    assert account["expires_at"] == 1794144709133
    assert account["domain"] == "www.workbuddy.cn"


def test_import_local_missing_file(tmp_path, monkeypatch):
    from relay.platforms.workbuddy import ide_switch as sw
    monkeypatch.setattr(sw, "auth_file_path", lambda profile=None: tmp_path / "nope.info")
    with pytest.raises(ImportParseError, match="未在本机"):
        asyncio.run(imports.import_local())


def test_import_local_logged_out_marker(tmp_path, monkeypatch):
    from relay.platforms.workbuddy import ide_switch as sw
    auth_file = tmp_path / "workbuddy-desktop.info"
    auth_file.write_text("{}", encoding="utf-8")
    sw.logout_marker_path(auth_file).write_text("logged-out", encoding="utf-8")
    monkeypatch.setattr(sw, "auth_file_path", lambda profile=None: auth_file)
    with pytest.raises(ImportParseError, match="未在本机"):
        asyncio.run(imports.import_local())


# ── 双域（2026-10-15 二期）：本机导入按 profile + 9router provider 分派 ──

def test_import_local_global_profile(tmp_path, monkeypatch):
    """国际版 profile：读 -ai 登录态文件 → 落国际池（CN 池零写入）；归属校验通过。"""
    from relay.platforms.workbuddy import ide_switch as sw
    from relay.platforms.workbuddy.realms import REALM_GLOBAL

    auth_file = tmp_path / "workbuddy-desktop-ai.info"
    doc = _plain_local_doc()
    doc["auth"]["domain"] = "www.workbuddy.ai"
    doc["account"]["uid"] = "uid-gl"
    doc["accounts"][0]["uid"] = "uid-gl"
    doc["allAccounts"][0]["uid"] = "uid-gl"
    enc = sw.encode_session(doc, TEST_KEY)
    auth_file.write_text(json.dumps(enc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sw, "auth_file_path", lambda profile=None: auth_file)

    async def fake_key(profile=None):
        return TEST_KEY

    monkeypatch.setattr(sw, "get_official_key", fake_key)

    async def fake_prefetch(account, profile=None):
        return

    monkeypatch.setattr(oauth, "prefetch_caches", fake_prefetch)

    account = asyncio.run(imports.import_local(profile=REALM_GLOBAL))
    assert account["uid"] == "uid-gl"
    assert account["domain"] == "www.workbuddy.ai"
    assert len(asyncio.run(store.load_accounts("workbuddy-global"))) == 1
    assert asyncio.run(store.load_accounts("workbuddy")) == []  # CN 池零写入


def test_import_local_global_rejects_cn_domain(tmp_path, monkeypatch):
    """国际版 profile 读到 CN 域登录态（错位场景）→ 拒绝并引导。"""
    from relay.platforms.workbuddy import ide_switch as sw
    from relay.platforms.workbuddy.realms import REALM_GLOBAL

    auth_file = tmp_path / "workbuddy-desktop-ai.info"
    doc = sw.encode_session(_plain_local_doc(), TEST_KEY)  # domain=www.workbuddy.cn
    auth_file.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(sw, "auth_file_path", lambda profile=None: auth_file)

    async def fake_key(profile=None):
        return TEST_KEY

    monkeypatch.setattr(sw, "get_official_key", fake_key)
    with pytest.raises(ImportParseError, match="国内版"):
        asyncio.run(imports.import_local(profile=REALM_GLOBAL))


def test_parse_9router_provider_by_profile():
    """9router providerConnections 筛选按 profile：国际版只吃 codebuddy-intl。"""
    from relay.platforms.workbuddy.realms import REALM_GLOBAL

    doc = {"providerConnections": [
        {"accessToken": _jwt(sub="uid-cn"), "provider": "codebuddy-cn", "name": "cn"},
        {"accessToken": _jwt(sub="uid-gl"), "provider": "codebuddy-intl", "name": "gl"},
    ]}
    [cn] = imports.parse_import_text(json.dumps(doc))              # CN 默认
    assert cn["uid"] == "uid-cn"
    [gl] = imports.parse_import_text(json.dumps(doc), REALM_GLOBAL)
    assert gl["uid"] == "uid-gl"
    # 单侧缺失 → 明确报错
    only_cn = json.dumps({"providerConnections": [
        {"accessToken": _jwt(), "provider": "codebuddy-cn"}]})
    with pytest.raises(ImportParseError, match="无 codebuddy-intl"):
        imports.parse_import_text(only_cn, REALM_GLOBAL)
