"""M8 支撑脚本：连真实数据目录（AppData 共享指针）的 logs 库做聚合核对。

无第三方依赖（仅 stdlib sqlite3 / pathlib）。用于「打开安装版后」抽查 /v1/stats
聚合结果与统计页数字是否一致。定位逻辑与壳侧 resolve_data_dir 一致：
优先读 AppData 内 data_path.txt 指针 → 命中共享数据目录；否则默认 app_data_dir/data。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def _app_data_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(base) / "com.coderproxy.desktop"


def resolve_data_dir() -> Path:
    app_data = _app_data_dir()
    pointer = app_data / "data_path.txt"
    default = app_data / "data"
    if pointer.exists():
        target = Path(pointer.read_text(encoding="utf-8").strip())
        if str(target) and target.is_dir():
            return target
    return default


_AGG_SELECT = (
    "COUNT(*) AS requests,"
    " SUM(CASE WHEN kind='chat_done' THEN 1 ELSE 0 END) AS success,"
    " SUM(CASE WHEN kind='chat_error' THEN 1 ELSE 0 END) AS failed,"
    " SUM(COALESCE(prompt_tokens,0)) AS prompt_tokens,"
    " SUM(COALESCE(completion_tokens,0)) AS completion_tokens,"
    " SUM(COALESCE(cached_tokens,0)) AS cached_tokens,"
    " SUM(COALESCE(reasoning_tokens,0)) AS reasoning_tokens,"
    " SUM(COALESCE(total_tokens,0)) AS total_tokens"
)

_GROUP_EXPR = {
    "model": "COALESCE(model,'')",
    "kind": "COALESCE(kind,'')",
    "day": "COALESCE(strftime('%Y-%m-%d', datetime(ts, 'localtime')),'')",
    "hour": "COALESCE(strftime('%Y-%m-%d %H:00', datetime(ts, 'localtime')),'')",
}


def main() -> int:
    data_dir = resolve_data_dir()
    db = data_dir / "coderproxy.db"
    print(f"[m8] 数据目录: {data_dir}")
    print(f"[m8] 日志库:   {db}")
    if not db.exists():
        print("[m8] 日志库不存在，可能尚无落库数据。")
        return 1

    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        total_rows = conn.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
        print(f"[m8] logs 总行数: {total_rows}\n")

        print("[m8] kind 分布:")
        for r in conn.execute(
            "SELECT kind, COUNT(*) n FROM logs GROUP BY kind ORDER BY n DESC"):
            print(f"     {r['kind']:<20} {r['n']}")

        for g in ("day", "hour", "model", "kind"):
            expr = _GROUP_EXPR[g]
            rows = conn.execute(
                f"SELECT {expr} AS key,{_AGG_SELECT} FROM logs "
                f"GROUP BY {expr} ORDER BY key").fetchall()
            tot = conn.execute(f"SELECT {_AGG_SELECT} FROM logs").fetchone()
            print(f"\n[m8] 按 {g} 聚合:")
            for r in rows:
                print(f"     {r['key']:<24} 请求={r['requests']:<4} "
                      f"成功={r['success']:<4} 失败={r['failed']:<3} "
                      f"token={r['total_tokens']}")
            print(f"     {'[全部]':<24} 请求={tot['requests']:<4} "
                  f"成功={tot['success']:<4} 失败={tot['failed']:<3} "
                  f"token={tot['total_tokens']}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
