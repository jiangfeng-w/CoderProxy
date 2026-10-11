"""WorkBuddy 官方客户端登录态读写与 IDE 账号切换。

本地修订：逻辑移植自 cockpit-tools（src-tauri/src/modules/workbuddy_auth_crypto.rs 与
workbuddy_account.rs，MIT），与原实现的差异：
- Rust 异步/tokio → Python asyncio.subprocess + asyncio.to_thread；密钥失败退避（1s）省略（GUI 低频操作）。
- 会话构造底座不同：原实现用导入时保存的 auth_raw/profile_raw 快照，本项目账号池不存官方
  raw 对象，改为**以本机 auth 文件现有 accounts/allAccounts 数组中同 uid 项为底座**（含官方
  最近写入的字段，信息比旧快照新鲜），无同 uid 项才走最小构造。
- 进程管理用 taskkill/Popen（原实现走实例管理 close_pid），按映像名全关（切号本就要求全关）。
- 仅支持 Windows（打包目标 triple x86_64-pc-windows-msvc）；非 win32 直接报错。

加密契约（WorkBuddy 5.6 字段加密，装机实测 2026-10-11，见 spec 文档「本机实测证据」）：
- 登录态文件 %LOCALAPPDATA%/CodeBuddyExtension/Data/Public/auth/workbuddy-desktop.info
- 字段级 AES-256-GCM：{"$wbEncrypted":1,"envelope":"<b64(Envelope JSON)>"}，
  Envelope={suite,keyId=sha256(key)[:16hex],nonce(12B),authTag(16B),ciphertext}
- key = sha256(atRestSecretKey utf8)；AAD 框架见 _aad；密钥经官方 exe 的
  Electron binding 派生（ELECTRON_RUN_AS_NODE=1，只读无 GUI）
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

AUTH_FILE_NAME = "workbuddy-desktop.info"
LOGOUT_MARKER_SUFFIX = ".logged-out"
KEY_TIMEOUT_S = 5.0
KEY_TTL_S = 60.0
TASKKILL_TIMEOUT_S = 20.0

# 官方客户端安装路径候选（对齐 cockpit-tools process_core_discovery 的候选序；
# 本机实测命中第一个）。env RELAY_WB_IDE_EXE 优先。
_EXE_CANDIDATES = (
    r"%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe",
    r"%PROGRAMFILES%\WorkBuddy\WorkBuddy.exe",
    r"%PROGRAMFILES(X86)%\WorkBuddy\WorkBuddy.exe",
)

_CREATE_NO_WINDOW = 0x08000000

# 官方客户端进程映像名（taskkill 按名关闭；/T 覆盖 crashpad/helper 子进程树）
CLIENT_IMAGE_NAME = "WorkBuddy.exe"


class IdeSwitchError(RuntimeError):
    """IDE 切号业务失败（客户端缺失/密钥/格式/hash 冲突）；路由层映射 409。"""


# ── 密钥派生（照抄 workbuddy_auth_crypto.rs KEY_SCRIPT/run_key_command） ──

# 载荷只走匿名管道：不落盘、不进参数/环境变量、不进日志；对 base64 文本做 sha256。
_KEY_SCRIPT = r"""
try {
  const c = require('crypto');
  const p = JSON.parse(process._linkedBinding('electron_browser_workbuddy_storage').loggerGet());
  if (p.version !== 1 || typeof p.atRestSecretKey !== 'string') process.exit(2);
  const b = Buffer.from(p.atRestSecretKey, 'base64');
  if (b.length !== 32 || b.toString('base64') !== p.atRestSecretKey || b.every(x => x === 0)) process.exit(2);
  const key = c.createHash('sha256').update(p.atRestSecretKey, 'utf8').digest();
  process.stdout.write(key.toString('base64'));
  key.fill(0); b.fill(0);
} catch (_) { process.exit(2); }
"""


class OfficialKey:
    """官方加密密钥（32B）+ keyId（sha256 前 16 hex，envelope 定位/AAD 组成）。"""

    __slots__ = ("bytes", "id")

    def __init__(self, raw: bytes) -> None:
        if len(raw) != 32:
            raise IdeSwitchError("WorkBuddy 官方加密密钥长度异常，已停止覆盖")
        self.bytes = raw
        self.id = hashlib.sha256(raw).hexdigest()[:16]

    def aad(self) -> bytes:
        # 逐字节对齐 OfficialKey::aad（workbuddy_auth_crypto.rs:49-60）
        out = b"WB-AAD\x00\x01"
        for value in ("WBEV1", "sym-v1"):
            out += len(value).to_bytes(4, "big") + value.encode()
        out += (1).to_bytes(4, "big")
        out += len(self.id).to_bytes(4, "big") + self.id.encode()
        out += bytes((2, 0, 0))  # 字段框架：无序列号/结束位
        return out


def resolve_client_exe() -> Path | None:
    """官方客户端 exe 定位：env 覆盖 → 候选路径展开探测。"""
    override = os.environ.get("RELAY_WB_IDE_EXE", "").strip()
    if override:
        p = Path(override)
        return p if p.is_file() else None
    for candidate in _EXE_CANDIDATES:
        p = Path(os.path.expandvars(candidate))
        if p.is_file():
            return p
    return None


def auth_file_path() -> Path | None:
    """登录态文件路径（Windows：%LOCALAPPDATA%/CodeBuddyExtension/Data/Public/auth/）。"""
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        return None
    return Path(local) / "CodeBuddyExtension" / "Data" / "Public" / "auth" / AUTH_FILE_NAME


def logout_marker_path(auth_file: Path) -> Path:
    return Path(f"{auth_file}{LOGOUT_MARKER_SUFFIX}")


_key_lock = asyncio.Lock()
_cached_key: OfficialKey | None = None
_cached_at = 0.0


def invalidate_key() -> None:
    """官方密钥变化（keyId 不匹配/客户端重装）后失效缓存，下次重新派生。"""
    global _cached_key, _cached_at
    _cached_key = None
    _cached_at = 0.0


async def get_official_key() -> OfficialKey:
    """取官方密钥：TTL 缓存 + 单飞派生（asyncio.Lock 即单飞，等待者复用新缓存）。"""
    global _cached_key, _cached_at
    async with _key_lock:
        if _cached_key is not None and time.monotonic() - _cached_at < KEY_TTL_S:
            return _cached_key
        exe = resolve_client_exe()
        if exe is None:
            raise IdeSwitchError("未找到 WorkBuddy 官方客户端，无法读写其登录态")
        env = dict(os.environ)
        env["ELECTRON_RUN_AS_NODE"] = "1"
        env.pop("NODE_OPTIONS", None)
        kwargs: dict = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
        try:
            proc = await asyncio.create_subprocess_exec(
                str(exe), "-e", _KEY_SCRIPT,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                **kwargs,
            )
        except OSError as e:
            raise IdeSwitchError(f"无法读取 WorkBuddy 官方密钥，请检查客户端安装路径：{e}") from e
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), KEY_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            raise IdeSwitchError("读取 WorkBuddy 官方密钥超时，请重试") from None
        if proc.returncode != 0:
            raise IdeSwitchError(
                "当前 WorkBuddy 客户端未提供可用的官方密钥接口，已停止覆盖，请更新客户端后重试"
            )
        if len(out) != 44:
            raise IdeSwitchError("WorkBuddy 官方加密密钥输出异常，已停止覆盖")
        try:
            key = OfficialKey(base64.b64decode(out, validate=True))
        except (ValueError, IdeSwitchError) as e:
            raise IdeSwitchError("WorkBuddy 官方加密密钥解析失败，已停止覆盖") from e
        _cached_key = key
        _cached_at = time.monotonic()
        return key


# ── $wbEncrypted 字段加解密（照抄 open/seal + 白名单路径策略） ──

_B64 = base64.b64encode
_UNB64 = base64.b64decode

# 加密字段白名单：auth 两个 token 字段 + 账号对象的三个 PII 字段（accounts/allAccounts 同）
_AUTH_FIELDS = ("accessToken", "refreshToken")
_ACCOUNT_FIELDS = ("phoneNumber", "departmentFullName", "nickname")
_ACCOUNT_CONTAINERS = ("account", "accounts", "allAccounts")


def contains_encrypted_wrapper(value: object) -> bool:
    """递归检测 JSON 树中是否存在 $wbEncrypted 包装。"""
    if isinstance(value, dict):
        if "$wbEncrypted" in value:
            return True
        return any(contains_encrypted_wrapper(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_encrypted_wrapper(v) for v in value)
    return False


def _decode_b64(encoded: str) -> bytes:
    if len(encoded) > 16 * 1024 * 1024:
        raise IdeSwitchError("WorkBuddy 官方加密字段异常，已停止覆盖")
    try:
        raw = _UNB64(encoded)
    except (ValueError, TypeError) as e:
        raise IdeSwitchError("WorkBuddy 官方加密字段校验失败，已停止覆盖") from e
    if _B64(raw).decode() != encoded:  # 标准 padding 严格校验（对齐 decode_base64）
        raise IdeSwitchError("WorkBuddy 官方加密字段校验失败，已停止覆盖")
    return raw


def _open_field(wrapper: dict, key: OfficialKey) -> str:
    """解一个 $wbEncrypted 包装 → 明文（照抄 OfficialKey::open）。"""
    if len(wrapper) != 2 or wrapper.get("$wbEncrypted") != 1:
        raise IdeSwitchError("WorkBuddy 官方加密格式不受支持，已停止覆盖，请更新后重试")
    encoded = wrapper.get("envelope")
    if not isinstance(encoded, str):
        raise IdeSwitchError("WorkBuddy 官方加密字段校验失败，已停止覆盖")
    try:
        envelope = json.loads(_decode_b64(encoded))
    except json.JSONDecodeError as e:
        raise IdeSwitchError("WorkBuddy 官方加密字段校验失败，已停止覆盖") from e
    if not isinstance(envelope, dict):
        raise IdeSwitchError("WorkBuddy 官方加密字段校验失败，已停止覆盖")
    if envelope.get("suite") != 1:
        raise IdeSwitchError("WorkBuddy 官方加密格式不受支持，已停止覆盖，请更新后重试")
    if envelope.get("keyId") != key.id:
        raise IdeSwitchError("WorkBuddy 官方加密密钥已变化，正在重新准备，请稍后重试")
    nonce = _decode_b64(envelope.get("nonce", ""))
    auth_tag = _decode_b64(envelope.get("authTag", ""))
    if len(nonce) != 12 or len(auth_tag) != 16:
        raise IdeSwitchError("WorkBuddy 官方加密字段校验失败，已停止覆盖")
    ciphertext = _decode_b64(envelope.get("ciphertext", "")) + auth_tag
    try:
        plain = AESGCM(key.bytes).decrypt(nonce, ciphertext, key.aad())
        return plain.decode("utf-8")
    except (InvalidTag, UnicodeDecodeError) as e:
        raise IdeSwitchError("WorkBuddy 官方加密字段校验失败，已停止覆盖") from e


def _seal_field(plaintext: str, key: OfficialKey) -> dict:
    """明文 → $wbEncrypted 包装（随机 nonce；照抄 OfficialKey::seal）。"""
    nonce = os.urandom(12)
    sealed = AESGCM(key.bytes).encrypt(nonce, plaintext.encode("utf-8"), key.aad())
    split = len(sealed) - 16
    envelope = {
        "suite": 1,
        "keyId": key.id,
        "nonce": _B64(nonce).decode(),
        "authTag": _B64(sealed[split:]).decode(),
        "ciphertext": _B64(sealed[:split]).decode(),
    }
    envelope_bytes = json.dumps(envelope, separators=(",", ":")).encode()
    return {"$wbEncrypted": 1, "envelope": _B64(envelope_bytes).decode()}


def _decode_fields(obj: dict, names: tuple[str, ...], key: OfficialKey) -> None:
    for name in names:
        field = obj.get(name)
        if isinstance(field, dict) and "$wbEncrypted" in field:
            obj[name] = _open_field(field, key)


def decode_session(value: dict, key: OfficialKey) -> dict:
    """解密会话白名单字段；解后仍残留包装（未知路径）→ 拒绝（绝不落未知明文）。"""
    decoded = json.loads(json.dumps(value))  # 深拷贝
    auth = decoded.get("auth")
    if isinstance(auth, dict):
        _decode_fields(auth, _AUTH_FIELDS, key)
    containers: list[dict] = []
    account = decoded.get("account")
    if isinstance(account, dict):
        containers.append(account)
    for name in ("accounts", "allAccounts"):
        items = decoded.get(name)
        if isinstance(items, list):
            containers.extend(item for item in items if isinstance(item, dict))
    for obj in containers:
        _decode_fields(obj, _ACCOUNT_FIELDS, key)
    if contains_encrypted_wrapper(decoded):
        raise IdeSwitchError("WorkBuddy 官方加密格式不受支持（未知加密路径），已停止覆盖")
    return decoded


def _encode_fields(obj: dict, names: tuple[str, ...], key: OfficialKey) -> None:
    for name in names:
        field = obj.get(name)
        if isinstance(field, str):
            obj[name] = _seal_field(field, key)
        elif field is not None:
            raise IdeSwitchError("WorkBuddy 官方加密字段类型异常，已停止覆盖")


def encode_session(value: dict, key: OfficialKey) -> dict:
    """加密会话白名单字段（入参必须是已解密的明文会话）。"""
    if contains_encrypted_wrapper(value):
        raise IdeSwitchError("WorkBuddy 登录态含未解密字段，已停止覆盖")
    encoded = json.loads(json.dumps(value))  # 深拷贝
    auth = encoded.get("auth")
    if isinstance(auth, dict):
        _encode_fields(auth, _AUTH_FIELDS, key)
    account = encoded.get("account")
    if isinstance(account, dict):
        _encode_fields(account, _ACCOUNT_FIELDS, key)
    for name in ("accounts", "allAccounts"):
        items = encoded.get(name)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    _encode_fields(item, _ACCOUNT_FIELDS, key)
    return encoded


# ── 会话构造（本地修订：底座 = 本机文件数组同 uid 项，见模块头差异说明） ──

_SCOPE_DEFAULT = "openid profile offline_access email"


def _build_account_value(account: dict, base: dict | None) -> dict:
    """目标账号对象：官方字段保留（base）+ 账号池字段覆盖 + 官方默认值。"""
    obj = dict(base or {})
    if account.get("uid"):
        obj["uid"] = account["uid"]
    if account.get("nickname"):
        obj["nickname"] = account["nickname"]
    if account.get("enterprise_id"):
        obj["enterpriseId"] = account["enterprise_id"]
    obj.setdefault("type", "personal")
    obj.setdefault("accountType", "")
    obj.setdefault("idp", "")
    obj.setdefault("oneidAccountId", "")
    obj.setdefault("areaInfoComplete", False)
    obj.setdefault("isCurrentOneIdEnterprise", False)
    obj.setdefault("isFirstLogin", False)
    obj.setdefault("deployStatus", {"statusCode": 0, "statusMsg": "", "detailMsg": ""})
    obj.setdefault("sso", {"domain": "", "domainModifiedTimes": 0})
    obj["lastLogin"] = True
    obj["pluginEnabled"] = True
    return obj


def _build_auth_value(account: dict) -> dict:
    """auth 根最小构造（对齐 build_default_auth_value 无 auth_raw 分支）。"""
    now_ms = int(time.time() * 1000)
    expires_at = int(account.get("expires_at") or 0)
    remaining_s = max(0, (expires_at - now_ms) // 1000) if expires_at > 0 else 0
    return {
        "accessToken": account.get("access_token", ""),
        "refreshToken": account.get("refresh_token") or "",
        "tokenType": "Bearer",
        "domain": account.get("domain") or "",
        "lastRefreshTime": now_ms,
        "expiresIn": remaining_s if expires_at > 0 else 0,
        "refreshExpiresIn": remaining_s if expires_at > 0 else 0,
        **({"expiresAt": expires_at, "refreshExpiresAt": expires_at} if expires_at > 0 else {}),
        "scope": _SCOPE_DEFAULT,
    }


def _merge_accounts(items: object, account_value: dict) -> list:
    """数组合并：同 uid 替换为目标值，其余 lastLogin=false，无则 append。"""
    target_uid = account_value.get("uid")
    merged: list = []
    found = False
    for item in items if isinstance(items, list) else []:
        if (
            not found
            and target_uid
            and isinstance(item, dict)
            and item.get("uid") == target_uid
        ):
            found = True
            merged.append(dict(account_value))
            continue
        if isinstance(item, dict):
            item = {**item, "lastLogin": False}
        merged.append(item)
    if not found:
        merged.append(dict(account_value))
    return merged


def build_session(account: dict, existing: dict | None) -> dict:
    """构造完整登录态：existing（已解密或 None）为底座 + 目标账号覆盖。"""
    existing = existing if isinstance(existing, dict) else {}
    uid = str(account.get("uid") or "")
    base = None
    for name in ("accounts", "allAccounts"):
        items = existing.get(name)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict) and str(item.get("uid") or "") == uid:
                base = item
                break
        if base is not None:
            break
    account_value = _build_account_value(account, base)
    session = dict(existing)
    session["account"] = dict(account_value)
    session["auth"] = _build_auth_value(account)
    session["accounts"] = _merge_accounts(existing.get("accounts"), account_value)
    session["allAccounts"] = _merge_accounts(existing.get("allAccounts"), account_value)
    return session


# ── 写入（照抄 write_account_to_auth_file：hash 前置原子写 + 回读校验 + 标记清理） ──


def _atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(path.name + ".coderproxy.tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _atomic_write_if_hash_matches(path: Path, content: str, expected: bytes) -> None:
    """hash 前置原子写：os.replace 前重读比对，官方客户端并发更新则拒绝。"""
    tmp = path.with_name(path.name + ".coderproxy.tmp")
    tmp.write_text(content, encoding="utf-8")
    try:
        current = path.read_bytes()
        if hashlib.sha256(current).digest() != expected:
            raise IdeSwitchError(
                "WorkBuddy 登录信息在切号期间被官方客户端更新，已停止覆盖，请重试"
            )
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _sha256_file(path: Path) -> bytes | None:
    try:
        return hashlib.sha256(path.read_bytes()).digest()
    except OSError:
        return None


def write_account_to_auth_file_sync(account: dict, auth_file: Path, key: OfficialKey) -> None:
    """把账号池凭证写入官方登录态文件（同步实现，编排层 asyncio.to_thread 调用）。

    步骤（spec「写入流程」7 步的 2–7）：读现有 → 解密 → 构造 → 加密 → hash 前置
    原子写 → 回读校验 → 删登出标记。
    """
    marker = logout_marker_path(auth_file)
    marker_hash = _sha256_file(marker) if marker.exists() else None
    existing_bytes: bytes | None = None
    if auth_file.exists():
        existing_bytes = auth_file.read_bytes()
    try:
        existing = json.loads(existing_bytes.decode("utf-8")) if existing_bytes else None
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise IdeSwitchError(f"现有 WorkBuddy 登录信息不是有效 JSON，已停止覆盖：{e}") from e

    encrypt = existing is not None and contains_encrypted_wrapper(existing)
    decoded_existing = decode_session(existing, key) if encrypt else existing
    session = build_session(account, decoded_existing)
    content_obj = encode_session(session, key) if encrypt else session
    content = json.dumps(content_obj, ensure_ascii=False, indent=2) + "\n"

    if existing_bytes is not None:
        _atomic_write_if_hash_matches(auth_file, content, hashlib.sha256(existing_bytes).digest())
    else:
        auth_file.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(auth_file, content)

    if (
        marker_hash is not None
        and marker.exists()
        and _sha256_file(marker) == marker_hash
    ):
        marker.unlink(missing_ok=True)

    # 回读校验：目标 token 已写入 + 官方四根字段齐全（校验失败不回滚——hash 前置已保证
    # 基线一致，失败说明写入通道异常，让用户重试而非静默假成功）
    try:
        written = json.loads(auth_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        raise IdeSwitchError(f"校验 WorkBuddy 登录信息失败：{e}") from e
    if contains_encrypted_wrapper(written):
        written = decode_session(written, key)
    written_auth = written.get("auth") if isinstance(written, dict) else None
    token = written_auth.get("accessToken") if isinstance(written_auth, dict) else None
    if token != account.get("access_token", ""):
        raise IdeSwitchError(f"校验 WorkBuddy 登录信息失败，未写入目标账号：{auth_file}")
    if isinstance(written, dict):
        for root in ("account", "auth", "accounts", "allAccounts"):
            if root not in written:
                raise IdeSwitchError(f"校验 WorkBuddy 登录信息失败，缺少官方字段 {root}")


# ── 状态读取（account.uid 明文，无需密钥）与进程管理 ──


def read_ide_state_sync() -> dict:
    """官方客户端安装/登录态概览（running 由编排层异步补充，此处不查进程）。"""
    exe = resolve_client_exe()
    auth_file = auth_file_path()
    marker = logout_marker_path(auth_file) if auth_file else None
    state: dict = {
        "installed": exe is not None,
        "exe_path": str(exe) if exe else "",
        "auth_exists": bool(auth_file and auth_file.exists()),
        "logged_out": bool(marker and marker.exists()),
        "current_uid": "",
    }
    if state["auth_exists"] and not state["logged_out"] and auth_file is not None:
        try:
            doc = json.loads(auth_file.read_text(encoding="utf-8"))
            account = doc.get("account")
            if isinstance(account, dict):
                state["current_uid"] = str(account.get("uid") or "")
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            pass  # 读取失败不阻塞概览；切号时会报具体错误
    return state


def is_client_running() -> bool:
    """官方客户端是否在运行（tasklist 按映像名查）。"""
    if sys.platform != "win32":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {CLIENT_IMAGE_NAME}"],
            check=False,
            capture_output=True, timeout=10, creationflags=_CREATE_NO_WINDOW,
        ).stdout.decode(errors="replace")
    except (OSError, subprocess.SubprocessError):
        return False
    return CLIENT_IMAGE_NAME.lower() in out.lower()


def close_client() -> None:
    """关闭官方客户端（taskkill /IM /T /F；对齐 cockpit-tools close_pid 的进程树强杀）。"""
    if sys.platform != "win32":
        raise IdeSwitchError("IDE 切号当前仅支持 Windows")
    try:
        subprocess.run(
            ["taskkill", "/IM", CLIENT_IMAGE_NAME, "/T", "/F"],
            check=False,
            capture_output=True, timeout=TASKKILL_TIMEOUT_S, creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise IdeSwitchError(f"关闭 WorkBuddy 客户端失败：{e}") from e


def start_client() -> None:
    """启动官方客户端（DETACHED | CREATE_NO_WINDOW，不等待退出）。"""
    exe = resolve_client_exe()
    if exe is None:
        raise IdeSwitchError("未找到 WorkBuddy 官方客户端，无法启动")
    if sys.platform != "win32":
        raise IdeSwitchError("IDE 切号当前仅支持 Windows")
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "creationflags": _CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
    }
    try:
        subprocess.Popen([str(exe)], cwd=str(exe.parent), **kwargs)
    except OSError as e:
        raise IdeSwitchError(f"启动 WorkBuddy 客户端失败：{e}") from e
