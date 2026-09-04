"""M7 造数脚本：向 coderproxy.db 批量插入演示 logs，供前端联调筛选/分页/近实时。

用法: python m7_logs_mock.py [--data-dir PATH] [--count N] [--models A,B,C] [--days N]
- --data-dir 缺省 = %APPDATA%/com.coderproxy.desktop/data（打包态共享指针目录，与 GUI 同库）。
- 插入内容为演示数据：chat_request/chat_done 成对 + 少量 chat_error，ts 分布在最近 N 天。
- 只增不删；联调完可在 GUI 日志页一键清空。
"""
import argparse
import asyncio
import os
import random
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
if not getattr(sys, "frozen", False):
    for _p in (_ROOT, _ROOT / "_deps"):
        _s = str(_p)
        if _s not in sys.path:
            sys.path.insert(0, _s)

from relay import db  # noqa: E402
from relay.config import settings  # noqa: E402

DEFAULT_MODELS = [
    "glm-5.3-flash", "glm-5.3", "kimi-k3",
    "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
]


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds")


async def _seed(count: int, models: list[str], days: int) -> int:
    now = datetime.now(timezone.utc)
    added = 0
    for i in range(count):
        model = models[i % len(models)]
        drift = random.uniform(0, days * 86400)
        base = now - timedelta(seconds=drift)
        stream = random.random() < 0.9
        kind = random.random()
        if kind < 0.45:  # chat_request + chat_done 成对（真实落库形态）
            tools = random.randint(0, 30)
            await db.log_event(kind="chat_request", model=model,
                               stream=1 if stream else 0,
                               ts=_iso(base),
                               detail={"tools": tools})
            done_at = base + timedelta(milliseconds=random.randint(300, 30000))
            prompt = random.randint(2_000, 200_000)
            completion = random.randint(10, 4000)
            await db.log_event(
                kind="chat_done", model=model, stream=1 if stream else 0,
                ts=_iso(done_at),
                prompt_tokens=prompt, completion_tokens=completion,
                cached_tokens=int(prompt * random.uniform(0, 0.99)),
                reasoning_tokens=random.randint(0, 1500),
                total_tokens=prompt + completion,
                duration_ms=int((done_at - base).total_seconds() * 1000))
            added += 2
        elif kind < 0.9:  # 单条 chat_request（本轮落库侧无 done，模拟进行中）
            await db.log_event(kind="chat_request", model=model,
                               stream=1 if stream else 0, ts=_iso(base),
                               detail={"tools": random.randint(0, 30)})
            added += 1
        else:  # chat_error（带上游摘要）
            await db.log_event(kind="chat_error", model=model,
                               stream=1 if stream else 0, ts=_iso(base),
                               detail={"error": "模型请求失败 504：上游网关超时（造数示例）"})
            added += 1
        if (i + 1) % 50 == 0:
            print(f"  ...{i + 1}/{count}")
    return added


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", default=os.path.expandvars(
        r"%APPDATA%\com.coderproxy.desktop\data"))
    ap.add_argument("--count", type=int, default=300, help="对数（成对时乘 2），默认 300")
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--days", type=int, default=7, help="时间跨度（天），默认 7")
    args = ap.parse_args()

    if args.data_dir == "%APPDATA%-missing":
        args.data_dir = str(Path(tempfile.mkdtemp(prefix="m7-mock-")))
    settings.data_dir = args.data_dir
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    if not models:
        models = DEFAULT_MODELS
    print(f"[m7] data_dir = {args.data_dir}")
    added = asyncio.run(_seed(args.count, models, args.days))
    total = asyncio.run(db.query_logs())[1]
    print(f"[m7] 新增 {added} 行，库内共 {total} 行")
    print("[m7] 完成：在 GUI 日志页筛选/分页即可看到效果；联调完可一键清空")
    return 0


if __name__ == "__main__":
    sys.exit(main())
