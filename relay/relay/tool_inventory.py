"""M11 工具指纹采集：多 agent 真实工具名发现（被动、落 SQLite）。

目标：任何 agent 通过 /v1 发来的 tools 声明（真实工具名 + 描述 + 参数 schema），
在伪装前被归一化后落库，作为后续「agent 工具 ↔ 牛码原生工具」语义映射的依据。
无需为每个 agent 写抓取脚本——relay 就是它们的统一出口，谁流过谁就被采集。

设计（对齐 db.py 惯例，同一 coderproxy.db）：
- 零第三方依赖：标准库 sqlite3，单文件库；防阻塞用 asyncio.to_thread。
- 表 tool_inventory：以 (name, param_sig) 唯一约束去重，重复声明累加 calls 频次。
- 归一化两种工具声明形态：OpenAI（type=function, function.name/parameters/description）
  与 Anthropic（type=tool, name/input_schema/description），统一收成
  {name, description, param_sig, schema_json}。

对外：
- record_tools(tools, model)   入站工具声明入库（调用方在伪装前、已判开关后调用）
- list_tools()                 按调用频次倒序返回指纹表（供映射/导出）
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from relay.config import settings
from relay.db import db_path

logger = logging.getLogger(__name__)

_INVENTORY_TABLE = "tool_inventory"

_CREATE_SQL = (
    f"CREATE TABLE IF NOT EXISTS {_INVENTORY_TABLE} (\n"
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    "  name TEXT NOT NULL,\n"
    "  param_sig TEXT NOT NULL,\n"
    "  description TEXT,\n"
    "  schema_json TEXT,\n"
    "  model TEXT,\n"
    "  first_ts TEXT,\n"
    "  last_ts TEXT,\n"
    "  calls INTEGER NOT NULL DEFAULT 1,\n"
    "  UNIQUE(name, param_sig)\n"
    ")"
)

# 唯一约束冲突时累加频次、更新最近一次信息（描述/schema 取最新可读）。
_UPSERT_SQL = (
    f"INSERT INTO {_INVENTORY_TABLE} "
    "(name, param_sig, description, schema_json, model, first_ts, last_ts, calls) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, 1) "
    f"ON CONFLICT(name, param_sig) DO UPDATE SET "
    " description = COALESCE(excluded.description, tool_inventory.description), "
    " schema_json = COALESCE(excluded.schema_json, tool_inventory.schema_json), "
    " model = excluded.model, "
    " last_ts = excluded.last_ts, "
    " calls = tool_inventory.calls + 1"
)

_init_lock = threading.Lock()
_initialized_path: str = ""


def _now_iso_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_SQL)
    conn.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{_INVENTORY_TABLE}_calls "
        f"ON {_INVENTORY_TABLE}(calls DESC)"
    )
    conn.commit()


def _schema_sync() -> None:
    """进程级 schema 初始化（按 db 路径记忆，换目录/换库自动重跑）。"""
    global _initialized_path
    path = str(db_path())
    if _initialized_path == path:
        return
    with _init_lock:
        if _initialized_path == path:
            return
        try:
            Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        conn = _connect()
        try:
            _ensure_schema(conn)
        finally:
            conn.close()
        _initialized_path = path


def _param_sig(params: Any) -> str:
    """由参数 schema 生成稳定签名，用于同一工具不同参数形态的去重区分。"""
    if not isinstance(params, dict):
        return ""
    props = params.get("properties")
    if not isinstance(props, dict):
        return ""
    parts = []
    for key in sorted(props):
        p = props[key]
        if isinstance(p, dict):
            t = p.get("type", "")
            parts.append(f"{key}:{t}" if t else key)
        else:
            parts.append(key)
    return "(" + ", ".join(parts) + ")"


def normalize_tools(tools: Any) -> list[dict[str, Any]]:
    """把入站 tools（OpenAI 或 Anthropic 形态）归一化成指纹字典列表。"""
    out: list[dict[str, Any]] = []
    for item in tools or []:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") or {}
        name = fn.get("name") or item.get("name") or ""
        if not name:
            continue
        description = fn.get("description") or item.get("description") or ""
        params = fn.get("parameters") or item.get("input_schema") or item.get("inputSchema") or None
        schema_json = json.dumps(params, ensure_ascii=False) if params is not None else ""
        out.append({
            "name": name,
            "description": description,
            "param_sig": _param_sig(params),
            "schema_json": schema_json,
        })
    return out


def _upsert_sync(tools: Any, model: str | None) -> int:
    _schema_sync()
    if not tools:
        return 0
    fingerprints = normalize_tools(tools)
    if not fingerprints:
        return 0
    ts = _now_iso_ms()
    conn = _connect()
    try:
        for fp in fingerprints:
            conn.execute(_UPSERT_SQL, (
                fp["name"],
                fp["param_sig"],
                fp["description"],
                fp["schema_json"],
                model or "",
                ts,
                ts,
            ))
        conn.commit()
        return len(fingerprints)
    finally:
        conn.close()


def _list_sync() -> list[dict[str, Any]]:
    _schema_sync()
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT name, param_sig, description, schema_json, model, "
            f"first_ts, last_ts, calls FROM {_INVENTORY_TABLE} "
            f"ORDER BY calls DESC, name ASC",
        ).fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if d.get("schema_json"):
            try:
                d["schema_json"] = json.loads(d["schema_json"])
            except (ValueError, TypeError):
                pass
        out.append(d)
    return out


async def record_tools(tools: Any, model: str | None) -> int:
    """入站工具声明入库；返回本请求采到的工具条数。写库在后台线程执行。"""
    return await asyncio.to_thread(_upsert_sync, tools, model)


async def list_tools() -> list[dict[str, Any]]:
    """返回工具指纹表（按调用频次倒序）。"""
    return await asyncio.to_thread(_list_sync)
