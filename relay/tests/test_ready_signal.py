"""就绪信号时机单测（BUG-009，2026-10-16）。

不变式：`[relay-ready]` 只在 uvicorn **真正 listen 之后**发出——即「就绪 ⇒ 端口可连」。
原实现打在 FastAPI startup 事件里（早于 listen ~150–200ms），GUI 落在该窗口的请求会
满屏假报错（`服务请求失败: error sending request for url`）。

覆盖：
- 原打印点 `routes._on_startup` 不再打就绪行（只做建库）；
- `cli._serve_announcing_ready`：`server.started` 为假时不 announce，转为真后恰好一次；
- 就绪行文本格式（壳解析协议）不回归；
- **集成回归锁**：真 uvicorn + 真「已绑定未 listen」回环 socket，announce 回调里立刻
  connect 必须成功（这正是本次修的窗口）。

全部 keyless、仅本机回环 socket，不触发上游网络。
"""
from __future__ import annotations

import asyncio
import re
import socket
import threading
import time

import pytest
import uvicorn

from relay import cli
from relay.config import settings


@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """隔离磁盘状态：建库不落真实数据目录。"""
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    return tmp_path


# ─────────────────────────── 原打印点已移除 ───────────────────────────

async def test_startup_hook_does_not_print_ready_line(isolated_data_dir, capsys):
    from relay import routes

    await routes._on_startup()

    assert "[relay-ready]" not in capsys.readouterr().out


# ─────────────────────────── announce 时机 ───────────────────────────

class _StubServer:
    """最小 uvicorn.Server 替身：n 次 tick 后置 started，再活一小会儿。"""

    def __init__(self, ticks_before_started: int = 2) -> None:
        self.started = False
        self._ticks = 0
        self._ticks_before_started = ticks_before_started
        self.served = False

    async def serve(self, sockets=None) -> None:  # noqa: ANN001（对齐 uvicorn 签名）
        for _ in range(self._ticks_before_started):
            await asyncio.sleep(0.01)
        self.started = True
        await asyncio.sleep(0.05)
        self.served = True


async def test_announce_after_started_exactly_once():
    server = _StubServer()
    seen: list[bool] = []

    await cli._serve_announcing_ready(server, [], lambda: seen.append(server.started))

    assert seen == [True]  # 恰好一次，且此刻 started 已为真
    assert server.served  # announce 不打断 serve


class _NeverStartedServer:
    """startup 失败形态：serve 直接抛错且 started 恒 False。"""

    started = False

    async def serve(self, sockets=None) -> None:  # noqa: ANN001
        raise RuntimeError("startup failed")


async def test_no_announce_when_startup_fails():
    server = _NeverStartedServer()
    seen: list[str] = []

    with pytest.raises(RuntimeError):
        await cli._serve_announcing_ready(server, [], lambda: seen.append("hit"))

    assert seen == []  # 起不来就不发就绪行（壳侧 60s 超时兜底报错）


# ─────────────────────────── 就绪行协议（壳解析） ───────────────────────────

def test_ready_line_format_unchanged(monkeypatch, capsys):
    monkeypatch.setattr(settings, "relay_port", 3601)
    monkeypatch.setattr(settings, "relay_api_key", "k" * 32)
    monkeypatch.setattr(settings, "tool_mode", "hybrid")

    cli._print_ready_line()

    line = capsys.readouterr().out.strip()
    assert re.fullmatch(r"\[relay-ready\] port=3601 api_key=\w+ tool_mode=hybrid", line)


# ─────────────────────────── 集成回归锁：就绪 ⇒ 端口可连 ───────────────────────────

async def _ok_app(scope, receive, send):  # noqa: ANN001（最小 ASGI app）
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


def test_ready_implies_connectable():
    """真 uvicorn 跑在「已绑定未 listen」的 socket 上：announce 一刻必须连得上。

    对齐生产路径（cli 用 pick_listen_socket 预绑定、由 uvicorn 负责 listen）——
    若就绪信号再提前到 listen 之前，本用例立刻红。
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    results: dict[str, str] = {}

    def announce() -> None:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                results["connect"] = "ok"
        except OSError as exc:  # pragma: no cover（回归时才会走到）
            results["connect"] = f"fail: {exc}"

    server = uvicorn.Server(uvicorn.Config(_ok_app, log_level="warning"))
    thread = threading.Thread(
        target=lambda: asyncio.run(cli._serve_announcing_ready(server, [sock], announce)),
        daemon=True,
    )
    thread.start()
    try:
        for _ in range(400):  # 最多等 20s
            if "connect" in results:
                break
            time.sleep(0.05)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()

    assert results.get("connect") == "ok", results
