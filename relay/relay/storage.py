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
    """首次启动：确保 config.api_key 已生成并回填 settings.relay_api_key。"""
    async with _asyncio_lock:
        state = await _io(_load_state)
        cfg = state.setdefault("config", {})
        key = cfg.get("api_key") or ""
        if not key:
            key = secrets.token_urlsafe(24)
            cfg["api_key"] = key
            state["config"] = cfg
            await _io(_save_state, state)
        if not settings.relay_api_key:
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
