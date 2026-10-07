"""自定义供应商配置存储（供应商页 F5：本期仅配置管理，转发不接）。

存储：独立 platform_custom.json，与 relay_state.json 完全隔离——对齐「WorkBuddy
多账号登录与签到」的存储纪律（独立文件 + 独立锁 + asyncio.to_thread + tmp.replace
原子写），但本模块只服务 /v1/providers/custom/* 管理端点。

数据结构：
{"version": 1, "providers": [{"id", "name", "base_url", "api_key", "enabled", "models"}]}

api_key 为敏感字段：仅本地落盘，任何 API 响应必须经 public_view() 序列化（不回显）。
转发生效依赖「WorkBuddy聊天反代与多平台聚合」的 adapter 注册表层，本期 build_provider
不改造。
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid
from pathlib import Path

from relay.config import settings

logger = logging.getLogger(__name__)

_FILENAME = "platform_custom.json"

# 独立锁实例（与 storage.py 的 relay_state.json 锁互不干扰）
_asyncio_lock = asyncio.Lock()
_file_lock = threading.Lock()


def _path() -> Path:
    return Path(settings.data_dir) / _FILENAME


def _load_sync() -> list[dict]:
    with _file_lock:
        p = _path()
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            logger.warning("[providers_custom] 配置文件损坏，按空配置处理: %s", p)
            return []
    providers = data.get("providers") if isinstance(data, dict) else None
    return providers if isinstance(providers, list) else []


def _save_sync(providers: list[dict]) -> None:
    with _file_lock:
        p = _path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        tmp = p.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"version": 1, "providers": providers}, ensure_ascii=False, indent=2),
            "utf-8")
        tmp.replace(p)


async def load_providers() -> list[dict]:
    """全部自定义供应商条目（含 api_key，仅 relay 内部用；对外响应走 public_view）。"""
    return await asyncio.to_thread(_load_sync)


async def find_provider(pid: str) -> dict | None:
    for p in await load_providers():
        if p.get("id") == pid:
            return p
    return None


async def upsert_provider(entry: dict) -> dict:
    """新增（无 id）或按 id 覆盖更新；返回落盘后的完整条目。id 不存在时抛 KeyError。"""
    async with _asyncio_lock:
        providers = await asyncio.to_thread(_load_sync)
        pid = entry.get("id")
        if not pid:
            entry = {**entry, "id": uuid.uuid4().hex[:12]}
            providers.append(entry)
        else:
            for i, p in enumerate(providers):
                if p.get("id") == pid:
                    providers[i] = entry
                    break
            else:
                raise KeyError(pid)
        await asyncio.to_thread(_save_sync, providers)
    return entry


async def delete_provider(pid: str) -> bool:
    async with _asyncio_lock:
        providers = await asyncio.to_thread(_load_sync)
        remaining = [p for p in providers if p.get("id") != pid]
        if len(remaining) == len(providers):
            return False
        await asyncio.to_thread(_save_sync, remaining)
        return True


def public_view(entry: dict) -> dict:
    """API 响应视图：剥离 api_key，以 has_api_key 表达「是否已设置」。"""
    return {
        "id": entry.get("id") or "",
        "name": entry.get("name") or "",
        "base_url": entry.get("base_url") or "",
        "enabled": bool(entry.get("enabled", True)),
        "models": [str(m) for m in (entry.get("models") or []) if str(m).strip()],
        "has_api_key": bool(entry.get("api_key")),
    }
