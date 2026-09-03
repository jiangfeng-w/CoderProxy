"""relay 运行监控（M4 GUI 日志面板数据源）。

- 事件：环形缓冲（默认 500 条），带单调递增 id，GUI 可用 after_id 增量拉取；
- 统计：按事件 kind 累加计数（请求/401 刷新/工具映射命中/长尾透传/错误等）。
- 线程安全：事件循环 + to_thread 双写，统一用 threading.Lock 保护。

对外：
- monitor.emit(kind, **data)  记录一条事件并累加计数
- monitor.stats()            当前计数快照
- monitor.events(limit, after_id)  增量事件列表（含 stats）
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class MonitorEvent:
    id: int
    ts: str
    kind: str
    data: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "ts": self.ts, "kind": self.kind, "data": self.data}


class Monitor:
    def __init__(self, capacity: int = 500):
        self._capacity = capacity
        self._events: deque[MonitorEvent] = deque(maxlen=capacity)
        self._stats: dict[str, int] = {}
        self._counter = 0
        self._lock = threading.Lock()

    def emit(self, kind: str, **data: Any) -> None:
        with self._lock:
            self._counter += 1
            self._events.append(MonitorEvent(self._counter, _now(), kind, data))
            self._stats[kind] = self._stats.get(kind, 0) + 1
            # 工具映射聚合：把每次 tool_disguise 事件的增量累加到独立计数，供 /v1/monitor/stats 汇总
            if kind == "tool_disguise":
                for k in ("map_hits", "longtail_passthrough", "dropped"):
                    v = data.get(k, 0)
                    if v:
                        self._stats["tool_" + k] = self._stats.get("tool_" + k, 0) + v

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    def events(self, limit: int = 200, after_id: int = 0) -> dict[str, Any]:
        """返回 (after_id 之后的) 事件列表 + 当前统计快照。"""
        with self._lock:
            items = [e.as_dict() for e in self._events if e.id > after_id]
            items = items[-max(1, min(limit, 1000)):]
            return {"events": items, "stats": dict(self._stats)}

    def clear(self) -> None:
        """清空事件缓冲与统计计数（GUI 日志页「清空」按钮）。"""
        with self._lock:
            self._events.clear()
            self._stats = {}
            self._counter = 0


monitor = Monitor()
