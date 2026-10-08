"""多协议端点兼容：真起服（uvicorn + TCP）冒烟——三协议端点 + 假上游。

与单测（TestClient，ASGI 内存传输）互补：本脚本走**真实 socket + 真实 uvicorn**，
验证三协议端点的 SSE 分帧在线路上的表现（分块、事件边界、错误体形状）。

不触碰牛码：起一个本地假上游（http.server）返回 OpenAI 风格 SSE，
并 monkeypatch 登录态为 logged_in（免账号）。

用法: py -3.13 proto_smoke.py   （在 relay/ 目录，或从仓库根跑）
"""
from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
for _p in (_ROOT, _ROOT / "_deps"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

API_KEY = "proto-smoke-key"

# ─────────────────────────── 假上游（OpenAI SSE） ───────────────────────────

_UPSTREAM_SSE = [
    'data: {"id":"chatcmpl-x","object":"chat.completion.chunk","model":"glm-x",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}',
    'data: {"id":"chatcmpl-x","object":"chat.completion.chunk","model":"glm-x",'
    '"choices":[{"index":0,"delta":{"reasoning_content":"想"},"finish_reason":null}]}',
    'data: {"id":"chatcmpl-x","object":"chat.completion.chunk","model":"glm-x",'
    '"choices":[{"index":0,"delta":{"content":"你好"},"finish_reason":null}]}',
    'data: {"id":"chatcmpl-x","object":"chat.completion.chunk","model":"glm-x",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}',
    "data: [DONE]",
]


class _FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for line in _UPSTREAM_SSE:
            self.wfile.write(f"{line}\n\n".encode())
            self.wfile.flush()
            time.sleep(0.005)
        self.wfile.write(b"")  # 收尾

    def log_message(self, *a):  # 静默
        return


def _start_fake_upstream() -> tuple[ThreadingHTTPServer, int]:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


# ─────────────────────────── relay 起服 ───────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="proto-smoke-")
    os.environ["RELAY_DATA_DIR"] = data_dir

    srv, upstream_port = _start_fake_upstream()

    import asyncio

    from relay import auth_flow, storage
    from relay.config import settings

    settings.data_dir = data_dir
    settings.relay_api_key = API_KEY
    settings.empty_stream_retries = 0  # 冒烟不重试，失败即失败

    asyncio.run(storage.save_models([{
        "name": "glm-x", "api_key": "llm-fake", "anthropic": False,
        "base_url": f"http://127.0.0.1:{upstream_port}",
        "completion_options": {}, "request_headers": {},
    }]))
    asyncio.run(storage.set_model_whitelist([]))

    async def _logged_in():
        return {"status": "logged_in"}

    auth_flow.login_status = _logged_in  # 免账号
    import relay.routes as routes_mod
    routes_mod.auth_flow.login_status = _logged_in

    port = _free_port()
    settings.relay_port = port

    import uvicorn
    from relay.routes import app

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)

    base = f"http://127.0.0.1:{port}"
    failures: list[str] = []

    def check(name: str, cond: bool, extra: str = "") -> None:
        detail = f"  | {extra}" if extra and not cond else ""
        print(("OK   " if cond else "FAIL ") + name + detail)
        if not cond:
            failures.append(name)

    def post(path: str, body: dict, *, headers: dict | None = None,
             timeout: float = 20) -> tuple[int, str]:
        if headers is None:
            headers = {"Authorization": f"Bearer {API_KEY}"}
        req = urllib.request.Request(
            base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", **headers},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")

    # ── 1. 既有 chat 端点不回归 ──
    code, text = post("/v1/chat/completions", {"model": "glm-x", "stream": True,
                                               "messages": [{"role": "user", "content": "hi"}]})
    check("chat 流式 200 + [DONE] + choices 形态",
          code == 200 and "data: [DONE]" in text and '"choices"' in text, f"code={code}")

    # ── 2. Responses 端点 ──
    code, text = post("/v1/responses", {"model": "glm-x", "stream": True, "input": "hi"})
    checks = ["event: response.created", "event: response.output_text.delta",
              "event: response.output_item.done", "event: response.completed"]
    check("responses 流式 typed 事件完整", code == 200 and all(c in text for c in checks),
          f"code={code} text={text[:300]}")
    check("responses 流式不泄漏 choices 形态", '"choices"' not in text)
    check("responses 流式 usage 非零（input_tokens=5）",
          '"input_tokens": 5' in text.replace(" ", " ") or '"input_tokens":5' in text)

    code, body = post("/v1/responses", {"model": "glm-x", "input": "hi"})
    j = json.loads(body) if code == 200 else {}
    check("responses 非流式 output[] 组装",
          code == 200 and j.get("object") == "response"
          and j.get("output", [{}])[-1].get("content", [{}])[0].get("text") == "你好",
          f"code={code} body={body[:300]}")

    # ── 3. Anthropic 端点（x-api-key）──
    code, text = post("/v1/messages",
                      {"model": "glm-x", "stream": True, "max_tokens": 128,
                       "messages": [{"role": "user", "content": "hi"}]},
                      headers={"x-api-key": API_KEY})
    checks = ["event: message_start", "event: content_block_start",
              "event: content_block_delta", "event: message_delta", "event: message_stop"]
    check("messages 流式 typed 事件完整（x-api-key 鉴权）",
          code == 200 and all(c in text for c in checks), f"code={code} text={text[:300]}")

    code, body = post("/v1/messages",
                      {"model": "glm-x", "max_tokens": 128,
                       "messages": [{"role": "user", "content": "hi"}]},
                      headers={"x-api-key": API_KEY})
    j = json.loads(body) if code == 200 else {}
    check("messages 非流式 content[] + stop_reason",
          code == 200 and j.get("type") == "message" and j.get("stop_reason") == "end_turn"
          and any(c.get("text") == "你好" for c in j.get("content", [])),
          f"code={code} body={body[:300]}")

    # ── 4. count_tokens + 鉴权 ──
    code, body = post("/v1/messages/count_tokens",
                      {"model": "glm-x", "messages": [{"role": "user", "content": "hello"}]},
                      headers={"x-api-key": API_KEY})
    check("count_tokens 返回 input_tokens", code == 200 and json.loads(body)["input_tokens"] >= 1)

    code, _ = post("/v1/responses", {"model": "glm-x", "input": "hi"}, headers={})
    check("responses 无鉴权 401", code == 401, f"code={code}")

    code, body = post("/v1/messages", {"model": "glm-x", "max_tokens": 8, "messages": []},
                      headers={"x-api-key": "wrong"})
    check("messages 错误 key 401", code == 401, f"code={code}")

    # ── 5. 未知模型 404（协议样式）──
    code, body = post("/v1/messages", {"model": "nope", "max_tokens": 8, "stream": True,
                                       "messages": [{"role": "user", "content": "hi"}]},
                      headers={"x-api-key": API_KEY})
    check("messages 未知模型非 200", code != 200, f"code={code}")

    srv.shutdown()
    server.should_exit = True
    print()
    if failures:
        print(f"FAILED {len(failures)}: {failures}")
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
