"""relay 日志 SQLite 持久化（M6 数据底座）。

- 零第三方依赖：标准库 sqlite3，单文件库落 `settings.data_dir/coderproxy.db`（WAL）。
- 主字段独立列（可索引，承担检索/筛选/聚合），`detail` 为 JSON 兜底口袋，
  不参与任何 WHERE/GROUP BY；新增扩展字段改 `COLUMN_DEFS` 即可，旧库自动升列。
- schema 自省补列：启动（或首次用）时 `PRAGMA table_info(logs)` 对比声明的列，
  缺列 `ALTER TABLE ... ADD COLUMN` 补齐——扩展列一律可空，旧行该列为 NULL。
- 防阻塞：同步 SQL 全部经 `asyncio.to_thread` 丢线程池执行；schema 初始化进程级一次。
- 保留策略：插入侧节流触发 prune，滚动删除最旧行（上限 `settings.relay_log_max_rows`）。
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

logger = logging.getLogger(__name__)

_DB_FILENAME = "coderproxy.db"
_LOG_TABLE = "logs"

# 声明列清单：(列名 -> 建表 SQL 片段)。自省补列时仅用列类型（可空）ALTER 补齐，
# 故扩展列一律不带 NOT NULL / UNIQUE（ADD COLUMN 约束，见 M6 spec §2.2）。
COLUMN_DEFS: dict[str, str] = {
    "ts": "TEXT NOT NULL",            # UTC ISO8601（毫秒）
    "kind": "TEXT NOT NULL",          # chat_request / chat_done / chat_error / auth_401_refresh ...
    "model": "TEXT",
    "stream": "INTEGER",              # 0/1
    "prompt_tokens": "INTEGER",
    "completion_tokens": "INTEGER",
    "cached_tokens": "INTEGER",
    "reasoning_tokens": "INTEGER",
    "total_tokens": "INTEGER",
    "duration_ms": "INTEGER",
    "detail": "TEXT",                 # JSON 兜底（如 error 摘要）
}

_ALTER_COLUMN_TYPES = {name: ddl.split()[0] for name, ddl in COLUMN_DEFS.items()}

_CREATE_SQL = (
    f"CREATE TABLE IF NOT EXISTS {_LOG_TABLE} (\n"
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,\n"
    + ",\n".join(f"  {name} {ddl}" for name, ddl in COLUMN_DEFS.items())
    + "\n)"
)

# 插入侧节流：每 N 次写触发一次 prune（避免每条 insert 都跑 COUNT 全表）
_PRUNE_EVERY = 32

_init_lock = threading.Lock()
_initialized_path: str = ""
_insert_count = 0
_insert_count_lock = threading.Lock()


def db_path() -> Path:
    return Path(settings.data_dir) / _DB_FILENAME


def _now_iso_ms() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path()), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """建表（幂等）+ 自省补列 + 建 ts 索引。"""
    conn.execute(_CREATE_SQL)
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({_LOG_TABLE})")}
    for name, typ in _ALTER_COLUMN_TYPES.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {_LOG_TABLE} ADD COLUMN {name} {typ}")
    conn.execute(f"CREATE INDEX IF NOT EXISTS idx_{_LOG_TABLE}_ts ON {_LOG_TABLE}(ts)")
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


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    detail = d.get("detail")
    if detail:
        try:
            d["detail"] = json.loads(detail)
        except (ValueError, TypeError):
            pass  # 保留原文串，前端自行兜底
    return d


def _log_event_sync(*, kind: str, **fields: Any) -> int:
    _schema_sync()
    if not kind:
        raise ValueError("kind 必填")
    ts = fields.pop("ts", None) or _now_iso_ms()
    detail = fields.pop("detail", None)
    if detail is not None and not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    cols = ["ts", "kind"]
    values: list[Any] = [ts, kind]
    for name in COLUMN_DEFS:
        if name in ("ts", "kind"):
            continue
        if name == "detail":
            v = detail
        else:
            v = fields.get(name)
        if v is None and name != "detail":
            continue
        cols.append(name)
        values.append(v)
    placeholders = ", ".join("?" for _ in cols)
    conn = _connect()
    try:
        cur = conn.execute(
            f"INSERT INTO {_LOG_TABLE} ({', '.join(cols)}) VALUES ({placeholders})",
            values,
        )
        conn.commit()
        row_id = int(cur.lastrowid)
    finally:
        conn.close()

    global _insert_count
    with _insert_count_lock:
        _insert_count += 1
        if _insert_count % _PRUNE_EVERY == 0:
            try:
                _prune_sync(settings.relay_log_max_rows)
            except Exception:  # noqa: BLE001（prune 失败不阻断写日志）
                pass
    return row_id


def _where_clause(kind: str | None = None, model: str | None = None,
                  time_from: str | None = None, time_to: str | None = None,
                  ) -> tuple[str, list[Any]]:
    conds: list[str] = []
    params: list[Any] = []
    if kind:
        conds.append("kind = ?")
        params.append(kind)
    if model:
        conds.append("model = ?")
        params.append(model)
    if time_from:
        conds.append("ts >= ?")
        params.append(time_from)
    if time_to:
        conds.append("ts <= ?")
        params.append(time_to)
    return (f" WHERE {' AND '.join(conds)}" if conds else ""), params


def _query_sync(limit: int = 50, offset: int = 0, kind: str | None = None,
                model: str | None = None, time_from: str | None = None,
                time_to: str | None = None) -> tuple[list[dict[str, Any]], int]:
    _schema_sync()
    where, params = _where_clause(kind, model, time_from, time_to)
    conn = _connect()
    try:
        total = conn.execute(
            f"SELECT COUNT(*) FROM {_LOG_TABLE}{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM {_LOG_TABLE}{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        return [_row_to_dict(r) for r in rows], int(total)
    finally:
        conn.close()


def _distinct_sync(column: str) -> list[str]:
    _schema_sync()
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT DISTINCT {column} FROM {_LOG_TABLE} "
            f"WHERE {column} IS NOT NULL AND {column} != '' ORDER BY {column}",
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _clear_sync(kind: str | None = None, model: str | None = None,
                time_from: str | None = None, time_to: str | None = None) -> int:
    _schema_sync()
    where, params = _where_clause(kind, model, time_from, time_to)
    conn = _connect()
    try:
        cur = conn.execute(f"DELETE FROM {_LOG_TABLE}{where}", params)
        conn.commit()
        return int(cur.rowcount)
    finally:
        conn.close()


def _prune_sync(max_rows: int) -> int:
    """超上限滚动删除最旧行；返回删除行数。"""
    _schema_sync()
    if max_rows <= 0:
        return 0
    conn = _connect()
    try:
        total = int(conn.execute(f"SELECT COUNT(*) FROM {_LOG_TABLE}").fetchone()[0])
        if total <= max_rows:
            return 0
        excess = total - max_rows
        conn.execute(
            f"DELETE FROM {_LOG_TABLE} WHERE id IN "
            f"(SELECT id FROM {_LOG_TABLE} ORDER BY id ASC LIMIT ?)",
            (excess,),
        )
        conn.commit()
        return int(excess)
    finally:
        conn.close()


# ─────────────────────────── 异步对外接口 ───────────────────────────

async def ensure_initialized() -> None:
    """建库 + schema 自省补列（sidecar 启动时调用一次）。"""
    await asyncio.to_thread(_schema_sync)


async def log_event(**fields: Any) -> int:
    """插入一条日志；返回行 id。写库在后台线程执行，不阻塞事件循环。"""
    return await asyncio.to_thread(_log_event_sync, **fields)


async def query_logs(limit: int = 50, offset: int = 0, kind: str | None = None,
                     model: str | None = None, time_from: str | None = None,
                     time_to: str | None = None) -> tuple[list[dict[str, Any]], int]:
    """分页 + 多条件筛选（时间/模型/类型可组合）；按 id 倒序返回 (rows, total)。"""
    return await asyncio.to_thread(
        _query_sync, limit, offset, kind, model, time_from, time_to)


async def distinct_kinds() -> list[str]:
    return await asyncio.to_thread(_distinct_sync, "kind")


async def distinct_models() -> list[str]:
    return await asyncio.to_thread(_distinct_sync, "model")


async def clear_logs(kind: str | None = None, model: str | None = None,
                     time_from: str | None = None, time_to: str | None = None) -> int:
    """按条件清空（无参数 = 全清）；返回删除行数。"""
    return await asyncio.to_thread(_clear_sync, kind, model, time_from, time_to)


async def prune(max_rows: int | None = None) -> int:
    """滚动删除最旧行至上限内；max_rows 缺省用 settings.relay_log_max_rows。"""
    return await asyncio.to_thread(
        _prune_sync, max_rows if max_rows is not None else settings.relay_log_max_rows)


# ─────────────────────────── 聚合统计（M8 /v1/stats）───────────────────────────

# 各维度分组表达式：day/hour 用 strftime 从 ts 切片；ts 落库为 UTC ISO8601，
# 展示/聚合按**本机时区**转本地（datetime(...,'localtime')），保证统计页时间与用户一致。
# model/kind 用独立列。全部包 COALESCE 兜 NULL（model/kind 允许为空、异常 ts 的 strftime 可能为 NULL）。
_STATS_GROUP_EXPR: dict[str, str] = {
    "model": "COALESCE(model, '')",
    "kind": "COALESCE(kind, '')",
    "day": "COALESCE(strftime('%Y-%m-%d', datetime(ts, 'localtime')), '')",
    "hour": "COALESCE(strftime('%Y-%m-%d %H:00', datetime(ts, 'localtime')), '')",
}

# 聚合 SELECT 片段（rows 与 total 共用；SUM 用 COALESCE 避免 NULL 参与求和）
_STATS_AGG_SELECT = (
    "COUNT(*) AS requests,"
    " SUM(CASE WHEN kind='chat_done' THEN 1 ELSE 0 END) AS success,"
    " SUM(CASE WHEN kind='chat_error' THEN 1 ELSE 0 END) AS failed,"
    " SUM(COALESCE(prompt_tokens, 0)) AS prompt_tokens,"
    " SUM(COALESCE(completion_tokens, 0)) AS completion_tokens,"
    " SUM(COALESCE(cached_tokens, 0)) AS cached_tokens,"
    " SUM(COALESCE(reasoning_tokens, 0)) AS reasoning_tokens,"
    " SUM(COALESCE(total_tokens, 0)) AS total_tokens"
)


def _stats_sync(group_by: str, model: str | None = None, kind: str | None = None,
                time_from: str | None = None, time_to: str | None = None,
                ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按维度聚合 token 与请求数；返回 (rows, total)。

    - rows：`GROUP BY <维度>` 后每组的 key 与各汇总字段，按 key 排序（ISO 时间/文本自然序）。
    - total：在同样 WHERE 过滤下、不分组的总量（供前端指标卡合计）。
    - 统计口径与时序检索一致：只针对 M6 独立列，`detail` JSON 不参与聚合。
    """
    _schema_sync()
    if group_by not in _STATS_GROUP_EXPR:
        raise ValueError(f"未知的聚合维度: {group_by}")
    expr = _STATS_GROUP_EXPR[group_by]

    where, params = _where_clause(kind=kind, model=model,
                                  time_from=time_from, time_to=time_to)
    conn = _connect()
    try:
        rows = conn.execute(
            f"SELECT {expr} AS key,{_STATS_AGG_SELECT} "
            f"FROM {_LOG_TABLE}{where} GROUP BY {expr} ORDER BY key",
            params).fetchall()
        total_row = conn.execute(
            f"SELECT {_STATS_AGG_SELECT} FROM {_LOG_TABLE}{where}",
            params).fetchone()
        row_list = [dict(r) for r in rows]
        # 聚合查询总存在 1 行；空表（或 WHERE 无命中）时 SUM 为 NULL，COUNT(*) 为 0，
        # 故统一把数值列 None 归 0，保证前端 total 口径稳定。
        _raw = dict(total_row) if total_row is not None else {}
        _zero = {"requests": 0, "success": 0, "failed": 0,
                 "prompt_tokens": 0, "completion_tokens": 0,
                 "cached_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0}
        total_dict = {k: (0 if _raw.get(k) is None else int(_raw[k])) for k in _zero}
        return row_list, total_dict
    finally:
        conn.close()


async def query_stats(group_by: str, model: str | None = None, kind: str | None = None,
                      time_from: str | None = None, time_to: str | None = None,
                      ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按维度聚合 token/请求数（M8 /v1/stats）；返回 (rows, total)。"""
    return await asyncio.to_thread(
        _stats_sync, group_by, model, kind, time_from, time_to)
