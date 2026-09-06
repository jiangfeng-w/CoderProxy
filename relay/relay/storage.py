"""relay 本地状态存储：单 JSON 文件（relay_state.json）持久化。

结构：
{
  "config":   {"api_key": "<RELAY_API_KEY>"},
  "ta3_auth": {"access_token": ..., "refresh_token": ..., "account": {...},
               "catalog": {...}, "updated_at": ...},
  "models":   [ {name/api_key/base_url/anthropic/...} ... ]
}

- M2 需求：token 落本地文件（非 DB），无需 sqlalchemy。
- 写盘用 asyncio.to_thread + 线程锁，避免阻塞事件循环与并发写坏文件。
- 目录与文件均为纯本机使用，权限不额外收紧（后续 M4 可迁 sqlite/加密）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from relay.config import settings

logger = logging.getLogger(__name__)

_STATE_FILENAME = "relay_state.json"

# 文件读写锁（跨协程 + to_thread 线程双保险）
_asyncio_lock = asyncio.Lock()
_file_lock = threading.Lock()


class Ta3AuthError(Exception):
    """ta3 认证错误。kind: login_required | refresh_failed | network"""

    def __init__(self, message: str, kind: str = "login_required"):
        super().__init__(message)
        self.kind = kind


@dataclass
class AuthRow:
    """ta3 登录态（对齐上游 Ta3Auth 行对象接口）。"""

    provider_id: int = 1
    access_token: str = ""
    refresh_token: str = ""
    account: dict = field(default_factory=dict)
    catalog: dict = field(default_factory=dict)
    updated_at: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def state_path() -> Path:
    return Path(settings.data_dir) / _STATE_FILENAME


def _load_state() -> dict:
    with _file_lock:
        p = state_path()
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            logger.warning("[storage] 状态文件损坏，按空状态处理: %s", p)
            return {}


def _save_state(state: dict) -> None:
    with _file_lock:
        p = state_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(p)


async def _io(fn, *args, **kwargs):
    """把同步文件操作丢到线程池，避免阻塞事件循环。"""
    return await asyncio.to_thread(fn, *args, **kwargs)


async def ensure_initialized() -> None:
    """首次启动：确保 config.api_key 已生成并回填 settings.relay_api_key。

    D1 装载语义：磁盘已有 key → 磁盘值优先（覆盖 env 兜底）；磁盘无 key 且
    已设 env RELAY_API_KEY → 以 env 为本次有效值（不落盘）；都无 → 生成并落盘。
    """
    async with _asyncio_lock:
        state = await _io(_load_state)
        cfg = state.setdefault("config", {})
        key = cfg.get("api_key") or ""
        if key:
            settings.relay_api_key = key
        elif settings.relay_api_key:
            pass  # env 兜底：磁盘无历史 key，本次以 env 值为有效值
        else:
            key = secrets.token_urlsafe(24)
            cfg["api_key"] = key
            state["config"] = cfg
            await _io(_save_state, state)
            settings.relay_api_key = key


def relay_api_key() -> str:
    """当前生效的 RELAY_API_KEY（环境变量优先，其次持久化值；无则惰性生成并落盘）。"""
    if settings.relay_api_key:
        return settings.relay_api_key
    state = _load_state()
    cfg = state.get("config") or {}
    key = cfg.get("api_key") or ""
    if not key:
        key = secrets.token_urlsafe(24)
        cfg["api_key"] = key
        state["config"] = cfg
        _save_state(state)
    settings.relay_api_key = key
    return key


async def save_api_key(key: str) -> None:
    """持久化静态 API Key（GUI/CLI 设置固定 key 用）。"""
    async with _asyncio_lock:
        state = await _io(_load_state)
        state.setdefault("config", {})["api_key"] = key
        await _io(_save_state, state)
        settings.relay_api_key = key


async def regenerate_api_key() -> str:
    """重置 API Key：生成新 key 落盘并回填 settings（GUI「重置密钥」按钮）。"""
    async with _asyncio_lock:
        key = secrets.token_urlsafe(24)
        state = await _io(_load_state)
        state.setdefault("config", {})["api_key"] = key
        await _io(_save_state, state)
        settings.relay_api_key = key
        return key


# ─────────────────────────── 监听端口（持久化）───────────────────────────

DEFAULT_PORT = 3601


async def get_port() -> int:
    """持久化的监听端口；未设置时返回默认 3601。

    GUI「配置页」改端口/重启均以本函数为准，确保每次启动同一个端口。
    """
    state = await _io(_load_state)
    p = (state.get("config") or {}).get("port")
    return p if isinstance(p, int) and 1 <= p <= 65535 else DEFAULT_PORT


async def save_port(port: int) -> int:
    """持久化监听端口（GUI 配置页写入）。"""
    async with _asyncio_lock:
        state = await _io(_load_state)
        state.setdefault("config", {})["port"] = int(port)
        await _io(_save_state, state)
    settings.relay_port = int(port)
    return int(port)


# ─────────────────────────── 工具伪装模式（持久化，D1）───────────────────────────

# 与 tool_disguise.VALID_MODES 保持一致（独立常量，避免模块环）
DEFAULT_TOOL_MODE = "hybrid"
_VALID_TOOL_MODES = {"hybrid", "strict", "passthrough"}


async def get_tool_mode() -> str:
    """磁盘持久化的工具伪装模式；未设置/非法时回退默认 hybrid。"""
    state = await _io(_load_state)
    m = (state.get("config") or {}).get("tool_mode")
    return m if m in _VALID_TOOL_MODES else DEFAULT_TOOL_MODE


async def save_tool_mode(mode: str) -> str:
    """持久化工具伪装模式（GUI/CLI 写入口；运行值由调用方同步 settings）。"""
    async with _asyncio_lock:
        state = await _io(_load_state)
        state.setdefault("config", {})["tool_mode"] = mode
        await _io(_save_state, state)
    return mode


async def apply_persisted_config() -> None:
    """D1 启动装载：磁盘持久化的 config 字段优先于 env/默认兜底。

    当前由磁盘装载的字段：tool_mode（磁盘未保存时保持 env/默认，不覆写）。
    api_key 由 ensure_initialized 装载、port 由 CLI get_port() 装载，各走各的
    读取路径，本函数只负责其余「持久化优先」的配置字段，供 cli._cmd_run 调用。
    """
    state = await _io(_load_state)
    m = (state.get("config") or {}).get("tool_mode")
    if m in _VALID_TOOL_MODES:
        settings.tool_mode = m


# ─────────────────────────── 每模型默认思考强度 ───────────────────────────

# agent 未显式传 thinking/reasoning_effort 时按此默认值下发；"none" = 关思考
# （走 ta3.py 的 thinking:disabled 路径）。未配置的模型按 "none" 处理。
DEFAULT_THINKING_EFFORT = "none"
_EFFORT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

# agent 不传思考参数时的兜底策略：
# - "default"（假关，默认）：按每模型 thinking_defaults 下发（模型页可调成低/高档）
# - "off"（真关）：一律显式关思考（thinking:disabled），忽略每模型默认值
VALID_THINKING_UNSET_MODES = {"default", "off"}
DEFAULT_THINKING_UNSET_MODE = "default"


def valid_thinking_effort(value) -> bool:
    """档位取值合法性：none 或牛码 thinkingLevels 的 level 形态（小写字母开头短串）。"""
    return isinstance(value, str) and bool(_EFFORT_RE.match(value))


async def get_thinking_defaults() -> dict[str, str]:
    """每模型默认思考强度映射 {model: effort}（只含合法条目）。"""
    state = await _io(_load_state)
    raw = (state.get("config") or {}).get("thinking_defaults") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items()
            if str(k).strip() and valid_thinking_effort(v)}


async def save_thinking_defaults(defaults: dict) -> dict[str, str]:
    """整体覆盖每模型默认思考强度映射（GUI 模型页下拉写入）。"""
    clean = {str(k): v for k, v in (defaults or {}).items()
             if str(k).strip() and valid_thinking_effort(v)}
    async with _asyncio_lock:
        state = await _io(_load_state)
        state.setdefault("config", {})["thinking_defaults"] = clean
        await _io(_save_state, state)
    return clean


async def get_thinking_unset_mode() -> str:
    """agent 不传思考参数时的兜底策略（default/off）；损坏值回退默认（假关）。"""
    state = await _io(_load_state)
    m = (state.get("config") or {}).get("thinking_unset_mode")
    return m if m in VALID_THINKING_UNSET_MODES else DEFAULT_THINKING_UNSET_MODE


async def save_thinking_unset_mode(mode: str) -> str:
    """持久化兜底策略（GUI 设置页「真关/假关」开关写入）。"""
    if mode not in VALID_THINKING_UNSET_MODES:
        mode = DEFAULT_THINKING_UNSET_MODE
    async with _asyncio_lock:
        state = await _io(_load_state)
        state.setdefault("config", {})["thinking_unset_mode"] = mode
        await _io(_save_state, state)
    return mode


# ─────────────────────────── 模型白名单（M4）───────────────────────────

# 哨兵：白名单为 [DISABLE_ALL] 表示「全部禁用」（区别于 [] 的全部启用）。
DISABLE_ALL = "__none__"


async def get_model_whitelist() -> list[str]:
    """已启用的模型名列表；空列表 = 全部启用（默认，兼容 M2/M3）。"""
    state = await _io(_load_state)
    wl = (state.get("config") or {}).get("model_whitelist") or []
    return wl if isinstance(wl, list) else []


async def set_model_whitelist(names: list[str]) -> list[str]:
    """写入白名单（GUI 模型页勾选结果）。传 [] 表示全部启用；[DISABLE_ALL] 表示全部禁用。"""
    clean = [str(n) for n in names if str(n).strip()]
    async with _asyncio_lock:
        state = await _io(_load_state)
        state.setdefault("config", {})["model_whitelist"] = clean
        await _io(_save_state, state)
    return clean


async def is_model_enabled(name: str) -> bool:
    """白名单为空 → 全部启用；[DISABLE_ALL] → 全部禁用；否则只放行列表内模型。"""
    wl = await get_model_whitelist()
    if not wl:
        return True
    if wl == [DISABLE_ALL]:
        return False
    return name in wl


# ─────────────────────────── ta3 登录态 ───────────────────────────

def _auth_from_state(state: dict, provider_id: int) -> AuthRow | None:
    a = state.get("ta3_auth")
    if not isinstance(a, dict) or not a.get("access_token"):
        return None
    return AuthRow(
        provider_id=provider_id,
        access_token=a.get("access_token") or "",
        refresh_token=a.get("refresh_token") or "",
        account=a.get("account") or {},
        catalog=a.get("catalog") or {},
        updated_at=a.get("updated_at") or "",
    )


async def load_auth(db=None, provider_id: int = 1) -> AuthRow | None:
    """db 参数仅为兼容 vendored oauth.py/catalog.py 的调用签名，忽略不传。"""
    state = await _io(_load_state)
    return _auth_from_state(state, provider_id)


async def get_auth_row(db=None, provider_id: int = 1) -> AuthRow:
    """取或建（惰性创建）登录态行。"""
    row = await load_auth(None, provider_id)
    if row is None:
        row = AuthRow(provider_id=provider_id, updated_at=_now())
    return row


async def save_auth(db=None, provider_id: int = 1, *, access_token: str,
                    refresh_token: str | None = None, account: dict | None = None,
                    catalog: dict | None = None) -> AuthRow:
    async with _asyncio_lock:
        state = await _io(_load_state)
        a = state.get("ta3_auth") or {}
        a["access_token"] = access_token
        if refresh_token is not None:
            a["refresh_token"] = refresh_token
        if account is not None:
            a["account"] = account
        if catalog is not None:
            a["catalog"] = catalog
        a["updated_at"] = _now()
        state["ta3_auth"] = a
        await _io(_save_state, state)
        return AuthRow(provider_id=provider_id, access_token=access_token,
                       refresh_token=a.get("refresh_token") or "",
                       account=a.get("account") or {}, catalog=a.get("catalog") or {},
                       updated_at=a["updated_at"])


async def clear_auth(db=None, provider_id: int = 1) -> None:
    async with _asyncio_lock:
        state = await _io(_load_state)
        if state.get("ta3_auth"):
            state.pop("ta3_auth", None)
            await _io(_save_state, state)


async def get_access_token(db=None, provider_id: int = 1) -> str | None:
    row = await load_auth(None, provider_id)
    return row.access_token if row else None


# ─────────────────────────── 模型目录 ───────────────────────────

async def load_models() -> list[dict]:
    state = await _io(_load_state)
    models = state.get("models") or []
    return models if isinstance(models, list) else []


async def save_models(models: list[dict]) -> None:
    async with _asyncio_lock:
        state = await _io(_load_state)
        state["models"] = models
        await _io(_save_state, state)


async def find_model(name: str) -> dict | None:
    models = await load_models()
    for m in models:
        if m.get("name") == name:
            return m
    return None
