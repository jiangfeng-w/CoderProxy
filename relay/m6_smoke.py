"""M6 冒烟：无真实登录态下的日志存储/API 落库链路（纯本地，不触网）。

用法: python m6_smoke.py [数据目录]
覆盖 M6 spec §5：冷启动建库 → 各 kind 落库 → /v1/logs 查询/筛选/清空 → prune。
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if not getattr(sys, "frozen", False):
    for _p in (_ROOT, _ROOT / "_deps"):
        _s = str(_p)
        if _s not in sys.path:
            sys.path.insert(0, _s)

from relay import db  # noqa: E402
from relay.config import settings  # noqa: E402


async def _smoke() -> int:
    data_dir = sys.argv[1] if len(sys.argv) > 1 else Path(tempfile.mkdtemp(prefix="m6-smoke-"))
    settings.data_dir = str(data_dir)
    Path(settings.data_dir).mkdir(parents=True, exist_ok=True)
    print(f"[m6] data_dir = {data_dir}")

    await db.ensure_initialized()
    assert db.db_path().exists(), "coderproxy.db 未创建"
    print(f"[m6] OK  建库 {db.db_path().name}（WAL）")

    await db.log_event(kind="chat_request", model="glm-5.3-flash", stream=1,
                       detail={"tools": 3})
    await db.log_event(kind="chat_done", model="glm-5.3-flash", stream=1,
                       prompt_tokens=120, completion_tokens=80, cached_tokens=20,
                       reasoning_tokens=30, total_tokens=200, duration_ms=3500)
    await db.log_event(kind="auth_401_refresh", model="glm-5.3-flash")
    await db.log_event(kind="chat_error", model="deepseek-v4-flash", stream=0,
                       detail={"error": "504 上游网关超时（冒烟示例）"})
    rows, total = await db.query_logs()
    assert total == 4, f"期望 4 行，实际 {total}"
    print(f"[m6] OK  落库 {total} 行（kind 各异）")

    done_rows, _ = await db.query_logs(kind="chat_done")
    assert done_rows[0]["prompt_tokens"] == 120
    assert done_rows[0]["duration_ms"] == 3500
    print("[m6] OK  chat_done usage 与 duration_ms 正确")

    kinds = await db.distinct_kinds()
    models = await db.distinct_models()
    assert set(kinds) == {"chat_request", "chat_done", "auth_401_refresh", "chat_error"}
    print(f"[m6] OK  kinds={kinds} models={models}")

    rows, total = await db.query_logs(limit=2, offset=1)
    assert total == 4 and len(rows) == 2, "分页不正确"
    print("[m6] OK  limit/offset 分页正确")

    _, total = await db.query_logs(model="deepseek-v4-flash", kind="chat_error")
    assert total == 1, "条件筛选不正确"
    print("[m6] OK  kind+model 组合筛选正确")

    deleted = await db.clear_logs(kind="chat_error")
    assert deleted == 1
    deleted_all = await db.clear_logs()
    assert deleted_all == 3
    _, total = await db.query_logs()
    assert total == 0
    print("[m6] OK  DELETE（条件 + 全清）正确")

    for i in range(5):
        await db.log_event(kind="chat_request", model=f"m{i}")
    pruned = await db.prune(max_rows=3)
    assert pruned == 2
    _, total = await db.query_logs()
    assert total == 3
    print("[m6] OK  prune 滚动删除最旧（删除 2 行，余 3）")

    print("[m6] PASS 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_smoke()))
