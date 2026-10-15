"""WorkBuddy IDE 切号单测：加解密（含 cockpit-tools 跨实现对齐向量）/ 会话构造 / 写入。

keyless：不依赖官方客户端、真实密钥或进程——密钥用合成 [7;32]，文件落 tmp_path，
密钥派生不触发（encode/decode 直接收 OfficialKey 对象）。
"""
import base64
import json
import time
from pathlib import Path

import pytest

from relay.platforms.workbuddy import ide_switch as sw
from relay.platforms.workbuddy.ide_switch import IdeSwitchError, OfficialKey

TEST_KEY = OfficialKey(bytes([7] * 32))  # 对齐 cockpit-tools with_test_key

# cockpit-tools workbuddy_auth_crypto.rs 测试 fixture：key=[7;32]、nonce=[3;12]，
# Rust/官方框架下 seal("fixture-token-测试") 的产物——Python 必须解出同明文。
COCKPIT_FIXTURE_ENVELOPE = (
    '{"suite":1,"keyId":"4bb06f8e4e3a7715","nonce":"AwMDAwMDAwMDAwMD",'
    '"authTag":"8mXI80XfjtTaNENkKD5E5Q==","ciphertext":"Q5fbdy9aO28OLyg5hXoZuWbUT3o="}'
)
COCKPIT_FIXTURE_WRAPPER = {
    "$wbEncrypted": 1,
    "envelope": base64.b64encode(COCKPIT_FIXTURE_ENVELOPE.encode()).decode(),
}


def _wrapper(nonce: bytes, tag: bytes, ciphertext: bytes, key_id: str, suite: int = 1) -> dict:
    envelope = {
        "suite": suite,
        "keyId": key_id,
        "nonce": base64.b64encode(nonce).decode(),
        "authTag": base64.b64encode(tag).decode(),
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }
    return {
        "$wbEncrypted": 1,
        "envelope": base64.b64encode(json.dumps(envelope).encode()).decode(),
    }


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


# ── 跨实现对齐向量与加解密 ──


def test_key_id_matches_cockpit_fixture():
    assert TEST_KEY.id == "4bb06f8e4e3a7715"


def test_cockpit_fixture_opens_to_same_plaintext():
    """Python 实现解出 Rust fixture 的明文——AAD 框架/envelope 逐字节一致。"""
    assert sw._open_field(COCKPIT_FIXTURE_WRAPPER, TEST_KEY) == "fixture-token-测试"


def test_seal_open_roundtrip():
    sealed = sw._seal_field("token-值-§", TEST_KEY)
    assert sw._open_field(sealed, TEST_KEY) == "token-值-§"
    assert sealed["$wbEncrypted"] == 1
    assert set(sealed) == {"$wbEncrypted", "envelope"}


def test_open_rejects_tampering_and_unknown_format():
    base = json.loads(base64.b64decode(COCKPIT_FIXTURE_WRAPPER["envelope"]))
    nonce, tag, ct = (base64.b64decode(base[k]) for k in ("nonce", "authTag", "ciphertext"))
    for field in ("nonce", "authTag", "ciphertext"):
        tampered = {**base, field: base64.b64encode(bytes(16)).decode()}
        wrapper = {
            "$wbEncrypted": 1,
            "envelope": base64.b64encode(json.dumps(tampered).encode()).decode(),
        }
        with pytest.raises(IdeSwitchError):
            sw._open_field(wrapper, TEST_KEY)
    # 错误密钥 → InvalidTag → 校验失败
    with pytest.raises(IdeSwitchError):
        sw._open_field(COCKPIT_FIXTURE_WRAPPER, OfficialKey(bytes([8] * 32)))
    # suite ≠ 1 / $wbEncrypted ≠ 1 → 不受支持
    with pytest.raises(IdeSwitchError, match="不受支持"):
        sw._open_field(_wrapper(nonce, tag, ct, base["keyId"], suite=2), TEST_KEY)
    with pytest.raises(IdeSwitchError):
        sw._open_field({**COCKPIT_FIXTURE_WRAPPER, "$wbEncrypted": 2}, TEST_KEY)
    # keyId 不匹配 → 密钥已变化
    with pytest.raises(IdeSwitchError, match="密钥已变化"):
        sw._open_field(_wrapper(nonce, tag, ct, "0" * 16), TEST_KEY)


def test_contains_encrypted_wrapper_nested():
    assert sw.contains_encrypted_wrapper({"a": [{"b": {"$wbEncrypted": 1}}]})
    assert not sw.contains_encrypted_wrapper({"a": [{"b": 1}]})
    assert not sw.contains_encrypted_wrapper("plain")


def test_decode_session_rejects_unknown_encrypted_path():
    session = {"auth": {"accessToken": "t"}, "unknown": {"secret": COCKPIT_FIXTURE_WRAPPER}}
    original = json.loads(json.dumps(session))
    with pytest.raises(IdeSwitchError, match="未知加密路径"):
        sw.decode_session(session, TEST_KEY)
    assert session == original  # 拒绝时不污染入参


def test_encode_session_rejects_undecoded_input():
    with pytest.raises(IdeSwitchError):
        sw.encode_session({"auth": {"accessToken": COCKPIT_FIXTURE_WRAPPER}}, TEST_KEY)


def test_encode_decode_session_whitelist_roundtrip():
    plain = {
        "auth": {"accessToken": "acc", "refreshToken": "ref", "expiresAt": 42},
        "account": {"uid": "one", "nickname": "测试", "phoneNumber": ""},
        "accounts": [{"uid": "other", "nickname": "Other", "lastLogin": False}],
        "allAccounts": [{"uid": "one", "nickname": "测试"}],
        "keep": {"revision": 1},  # 白名单外的明文字段原样保留
    }
    encoded = sw.encode_session(plain, TEST_KEY)
    assert isinstance(encoded["auth"]["accessToken"], dict)
    assert isinstance(encoded["accounts"][0]["nickname"], dict)
    assert encoded["auth"]["expiresAt"] == 42
    assert encoded["keep"] == {"revision": 1}
    decoded = sw.decode_session(encoded, TEST_KEY)
    assert decoded == plain
    assert sw.encode_session(plain, TEST_KEY) != encoded  # 随机 nonce


# ── 会话构造 ──


def test_build_session_merges_base_and_flips_last_login():
    existing = {
        "account": {"uid": "old", "nickname": "旧号", "lastLogin": True, "uin": "1"},
        "accounts": [
            {"uid": "old", "nickname": "旧号", "lastLogin": True, "uin": "1"},
            {"uid": "uid-a", "nickname": "保留字段", "uin": "2", "mpOpenId": "x"},
        ],
        "allAccounts": [{"uid": "old", "nickname": "旧号", "lastLogin": True}],
        "extraRoot": "保留",
    }
    session = sw.build_session(_account(), existing)
    # account 根以数组同 uid 项为底座（官方字段 uin/mpOpenId 保留）
    assert session["account"]["uin"] == "2"
    assert session["account"]["mpOpenId"] == "x"
    assert session["account"]["lastLogin"] is True
    assert session["account"]["pluginEnabled"] is True
    # accounts：目标项替换，其余 lastLogin 翻转
    by_uid = {item["uid"]: item for item in session["accounts"]}
    assert by_uid["old"]["lastLogin"] is False
    assert by_uid["uid-a"]["uin"] == "2"
    # allAccounts 无目标项 → append
    assert [item["uid"] for item in session["allAccounts"]] == ["old", "uid-a"]
    assert session["allAccounts"][0]["lastLogin"] is False
    # 顶层未知字段保留
    assert session["extraRoot"] == "保留"


def test_build_session_minimal_construction():
    session = sw.build_session(_account(), None)
    account = session["account"]
    assert account["type"] == "personal"
    assert account["lastLogin"] is True
    assert account["deployStatus"] == {"statusCode": 0, "statusMsg": "", "detailMsg": ""}
    assert account["sso"] == {"domain": "", "domainModifiedTimes": 0}
    auth = session["auth"]
    assert auth["accessToken"] == "tok-a"
    assert auth["refreshToken"] == "ref-a"
    assert auth["tokenType"] == "Bearer"
    assert auth["scope"] == "openid profile offline_access email"
    assert auth["expiresAt"] == 1794144709133
    assert auth["expiresIn"] > 0
    assert auth["refreshExpiresAt"] == 1794144709133
    assert {"accounts", "allAccounts"} <= set(session)
    assert [item["uid"] for item in session["accounts"]] == ["uid-a"]


def test_build_session_account_defaults_when_fields_missing():
    session = sw.build_session({"uid": "u", "access_token": "t", "expires_at": 0}, None)
    assert session["auth"]["expiresIn"] == 0
    assert "expiresAt" not in session["auth"]


# ── 写入流程 ──


def _plain_auth_file(uid: str = "old") -> dict:
    return {
        "account": {"uid": uid, "nickname": "旧号", "lastLogin": True},
        "auth": {"accessToken": "old-token", "expiresIn": 100},
        "accounts": [{"uid": uid, "nickname": "旧号", "lastLogin": True}],
        "allAccounts": [{"uid": uid, "nickname": "旧号", "lastLogin": True}],
    }


def test_write_plaintext_file_roundtrip(tmp_path):
    auth_file = tmp_path / "workbuddy-desktop.info"
    auth_file.write_text(json.dumps(_plain_auth_file()), encoding="utf-8")
    sw.write_account_to_auth_file_sync(_account(), auth_file, TEST_KEY)
    written = json.loads(auth_file.read_text(encoding="utf-8"))
    assert written["auth"]["accessToken"] == "tok-a"
    assert written["account"]["uid"] == "uid-a"
    # 明文底座 → 明文写出（不凭空引入加密）
    assert not sw.contains_encrypted_wrapper(written)


def test_write_encrypted_file_roundtrip(tmp_path):
    auth_file = tmp_path / "workbuddy-desktop.info"
    encrypted = sw.encode_session(_plain_auth_file(), TEST_KEY)
    auth_file.write_text(json.dumps(encrypted, ensure_ascii=False), encoding="utf-8")
    sw.write_account_to_auth_file_sync(_account(), auth_file, TEST_KEY)
    written = json.loads(auth_file.read_text(encoding="utf-8"))
    assert sw.contains_encrypted_wrapper(written)
    decoded = sw.decode_session(written, TEST_KEY)
    assert decoded["auth"]["accessToken"] == "tok-a"
    assert decoded["account"]["uid"] == "uid-a"
    by_uid = {item["uid"]: item for item in decoded["accounts"]}
    assert by_uid["uid-a"]["nickname"] == "账号甲"


def test_write_creates_file_when_missing(tmp_path):
    auth_file = tmp_path / "auth" / "workbuddy-desktop.info"
    sw.write_account_to_auth_file_sync(_account(), auth_file, TEST_KEY)
    written = json.loads(auth_file.read_text(encoding="utf-8"))
    assert written["auth"]["accessToken"] == "tok-a"
    for root in ("account", "auth", "accounts", "allAccounts"):
        assert root in written


def test_write_rejects_invalid_json_base(tmp_path):
    auth_file = tmp_path / "workbuddy-desktop.info"
    auth_file.write_text("not-json{", encoding="utf-8")
    with pytest.raises(IdeSwitchError, match="不是有效 JSON"):
        sw.write_account_to_auth_file_sync(_account(), auth_file, TEST_KEY)


def test_write_hash_precondition_rejects_concurrent_update(tmp_path, monkeypatch):
    auth_file = tmp_path / "workbuddy-desktop.info"
    auth_file.write_text(json.dumps(_plain_auth_file()), encoding="utf-8")
    # 模拟 replace 前重读时官方客户端已改写：第 1 次读=写流程读基线，第 2 次读=重读对比 → 返回异内容
    original_read_bytes = Path.read_bytes
    reads = {"n": 0}

    def racing_read_bytes(self, *args, **kwargs):
        data = original_read_bytes(self, *args, **kwargs)
        if self == auth_file:
            reads["n"] += 1
            if reads["n"] >= 2:
                racing = json.loads(data)
                racing["auth"]["accessToken"] = "client-rewrote"
                return json.dumps(racing).encode()
        return data

    monkeypatch.setattr(sw.Path, "read_bytes", racing_read_bytes)
    with pytest.raises(IdeSwitchError, match="被官方客户端更新"):
        sw.write_account_to_auth_file_sync(_account(), auth_file, TEST_KEY)


def test_write_cleans_logout_marker(tmp_path):
    auth_file = tmp_path / "workbuddy-desktop.info"
    auth_file.write_text(json.dumps(_plain_auth_file()), encoding="utf-8")
    marker = sw.logout_marker_path(auth_file)
    marker.write_text("logged-out", encoding="utf-8")
    sw.write_account_to_auth_file_sync(_account(), auth_file, TEST_KEY)
    assert not marker.exists()


def test_write_keeps_marker_when_changed(tmp_path, monkeypatch):
    """标记在写入窗口被官方客户端改动（hash 不匹配）→ 保留不删。"""
    auth_file = tmp_path / "workbuddy-desktop.info"
    auth_file.write_text(json.dumps(_plain_auth_file()), encoding="utf-8")
    marker = sw.logout_marker_path(auth_file)
    marker.write_text("logged-out", encoding="utf-8")

    original_read_bytes = Path.read_bytes
    marker_reads = {"n": 0}

    def tracking_read_bytes(self, *args, **kwargs):
        data = original_read_bytes(self, *args, **kwargs)
        if self == marker:
            marker_reads["n"] += 1
            if marker_reads["n"] >= 2:  # 第 1 次=写前记录 hash，第 2 次=写后对比
                return b"changed-by-client"
        return data

    monkeypatch.setattr(sw.Path, "read_bytes", tracking_read_bytes)
    sw.write_account_to_auth_file_sync(_account(), auth_file, TEST_KEY)
    assert marker.read_text(encoding="utf-8") == "logged-out"  # hash 不匹配 → 保留


def test_read_ide_state_sync(tmp_path, monkeypatch):
    auth_file = tmp_path / "CodeBuddyExtension" / "Data" / "Public" / "auth" / "workbuddy-desktop.info"
    auth_file.parent.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(sw, "resolve_client_exe", lambda profile=None: Path("C:/fake/WorkBuddy.exe"))
    doc = _plain_auth_file()
    doc["account"]["uid"] = "uid-live"
    auth_file.write_text(json.dumps(doc), encoding="utf-8")
    state = sw.read_ide_state_sync()
    assert state == {
        "installed": True,
        "exe_path": str(Path("C:/fake/WorkBuddy.exe")),
        "auth_exists": True,
        "logged_out": False,
        "current_uid": "uid-live",
    }
    # 登出标记存在 → 不读 uid
    sw.logout_marker_path(auth_file).write_text("x", encoding="utf-8")
    state = sw.read_ide_state_sync()
    assert state["logged_out"] is True
    assert state["current_uid"] == ""
    # 文件缺失
    auth_file.unlink()
    state = sw.read_ide_state_sync()
    assert state["auth_exists"] is False
    assert state["current_uid"] == ""


def test_official_key_rejects_wrong_length():
    with pytest.raises(IdeSwitchError):
        OfficialKey(bytes(31))


# ── 双域 profile（2026-10-15 二期：国际版客户端登录态/进程/exe 参数化） ──

def test_realm_profile_client_integration_fields():
    """profile 客户端集成字段：CN/国际版路径与进程名（本机实测形态）。"""
    from relay.platforms.workbuddy.realms import REALM_CN, REALM_GLOBAL

    assert REALM_CN.auth_file_name == "workbuddy-desktop.info"
    assert REALM_CN.client_image_name == "WorkBuddy.exe"
    assert REALM_CN.exe_env_override == "RELAY_WB_IDE_EXE"
    assert REALM_GLOBAL.auth_file_name == "workbuddy-desktop-ai.info"
    assert REALM_GLOBAL.client_image_name == "WorkBuddyAI.exe"
    assert REALM_GLOBAL.exe_env_override == "RELAY_WB_AI_IDE_EXE"
    assert any("WorkBuddyAI" in c for c in REALM_GLOBAL.exe_candidates)


def test_auth_file_path_dual_realm(tmp_path, monkeypatch):
    """两域登录态文件同目录并列、文件名按 profile 分派。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    from relay.platforms.workbuddy.realms import REALM_CN, REALM_GLOBAL

    cn = sw.auth_file_path(REALM_CN)
    gl = sw.auth_file_path(REALM_GLOBAL)
    assert cn.name == "workbuddy-desktop.info"
    assert gl.name == "workbuddy-desktop-ai.info"
    assert cn.parent == gl.parent  # 同目录并列（本机实测）


def test_read_ide_state_sync_global_profile(tmp_path, monkeypatch):
    """国际版 profile：从 -ai 文件读 current_uid；CN profile 看不到国际版文件。"""
    from relay.platforms.workbuddy.realms import REALM_CN, REALM_GLOBAL

    auth_file = (tmp_path / "CodeBuddyExtension" / "Data" / "Public" / "auth"
                 / "workbuddy-desktop-ai.info")
    auth_file.parent.mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(
        sw, "resolve_client_exe",
        lambda profile=None: Path("D:/Programs/WorkBuddyAI/WorkBuddyAI.exe"))
    doc = _plain_auth_file()
    doc["account"]["uid"] = "uid-global"
    auth_file.write_text(json.dumps(doc), encoding="utf-8")
    state = sw.read_ide_state_sync(REALM_GLOBAL)
    assert state["installed"] is True
    assert state["auth_exists"] is True
    assert state["current_uid"] == "uid-global"
    # CN profile 查的是 workbuddy-desktop.info（不存在 → auth_exists False）
    assert sw.read_ide_state_sync(REALM_CN)["auth_exists"] is False


def test_key_cache_isolated_per_platform():
    """密钥缓存按 platform_id 分桶（invalidate 单域不清另一域）。"""
    from relay.platforms.workbuddy.realms import REALM_CN, REALM_GLOBAL

    sw._cached_keys[REALM_CN.platform_id] = OfficialKey(bytes([7] * 32))
    sw._cached_keys[REALM_GLOBAL.platform_id] = OfficialKey(bytes([9] * 32))
    sw._cached_at[REALM_CN.platform_id] = time.monotonic()
    sw._cached_at[REALM_GLOBAL.platform_id] = time.monotonic()
    try:
        sw.invalidate_key(REALM_CN.platform_id)
        assert REALM_CN.platform_id not in sw._cached_keys
        assert REALM_GLOBAL.platform_id in sw._cached_keys
        sw.invalidate_key()  # 全清
        assert not sw._cached_keys
    finally:
        sw._cached_keys.clear()
        sw._cached_at.clear()


def test_resolve_client_exe_env_override_dual_realm(tmp_path, monkeypatch):
    """env 覆盖按 profile 分派（CN RELAY_WB_IDE_EXE / 国际版 RELAY_WB_AI_IDE_EXE）。"""
    from relay.platforms.workbuddy.realms import REALM_CN, REALM_GLOBAL

    fake_cn = tmp_path / "WorkBuddy.exe"
    fake_gl = tmp_path / "WorkBuddyAI.exe"
    fake_cn.write_text("x")
    fake_gl.write_text("x")
    monkeypatch.setenv("RELAY_WB_IDE_EXE", str(fake_cn))
    monkeypatch.setenv("RELAY_WB_AI_IDE_EXE", str(fake_gl))
    assert sw.resolve_client_exe(REALM_CN) == fake_cn
    assert sw.resolve_client_exe(REALM_GLOBAL) == fake_gl
