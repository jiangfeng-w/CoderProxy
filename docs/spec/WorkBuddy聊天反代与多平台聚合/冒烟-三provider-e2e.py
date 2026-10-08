#!/usr/bin/env python
"""三 provider 聚合 e2e 冒烟（「WorkBuddy聊天反代与多平台聚合」步骤 8 / §5 验收）。

在**隔离数据目录**（系统临时目录）起真 relay 产物（默认打包 exe，可切源码态），
用**已有小号账号池**（复制用户 platform_workbuddy.json，只读源文件）跑通：

- A 段（多协议入站验收）：`/v1/responses`、`/v1/messages`、`/v1/chat/completions`
  三协议经 WorkBuddy 上游各一次；外加工具调用透传；
- 聚合层：三 provider 目录合并（牛码 / WorkBuddy / 自定义）、白名单语义、
  未启用 404、未知前缀 404、WorkBuddy 无账号 503；
- 牛码回归：登录态 + 前缀目录 + 单模型端点（**不触发真实对话**——主账号封禁，
  规则 5 只允许小号，真实对话待解封用小号补测）。

安全：
- 只读用户数据目录（copy 到临时目录后读写临时副本），不写用户实时状态；
- WorkBuddy 只打 3~4 次对话（小号）；断网/限额时按失败处理并提示；
- relay 进程用进程树方式清理（onefile 是两层进程，防残留锁 exe）。

用法：
  py -3.13 "docs/spec/WorkBuddy聊天反代与多平台聚合/冒烟-三provider-e2e.py"
  py -3.13 .../冒烟-三provider-e2e.py --source   # 源码态（调 relay/run.py）替代 exe
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
APPDATA_DATA = Path(os.environ["APPDATA"]) / "com.coderproxy.desktop" / "data"
KEY_PATH = APPDATA_DATA / "relay_state.json"
WB_PATH = APPDATA_DATA / "platform_workbuddy.json"

WB_MODEL = "WorkBuddy/glm-5.3-flash"
CUSTOM_NAME = "基元律动"
CUSTOM_MODEL = "glm-5.3-flash"


# ─────────────────────────── 假自定义供应商上游 ───────────────────────────

_UPSTREAM_SSE = [
    'data: {"id":"up-1","object":"chat.completion.chunk","created":1,"model":"fake-1",'
    '"choices":[{"index":0,"delta":{"role":"assistant","content":"来自假上游"}}]}',
    'data: {"id":"up-1","object":"chat.completion.chunk","created":1,"model":"fake-1",'
    '"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}',
    "data: [DONE]",
]


class _FakeUpstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    seen: dict = {"auth": None, "model": None, "path": None}

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _FakeUpstream.seen["auth"] = self.headers.get("authorization")
        _FakeUpstream.seen["model"] = body.get("model")
        _FakeUpstream.seen["path"] = self.path
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # SSE 无 Content-Length；显式关连接（否则 HTTP/1.1 keep-alive 下客户端等超时）
        self.send_header("Connection", "close")
        self.end_headers()
        for line in _UPSTREAM_SSE:
            self.wfile.write(f"{line}\n\n".encode())
        self.close_connection = True

    def log_message(self, *a):  # noqa: ANN002
        pass


# ─────────────────────────── relay 起的隔离实例 ───────────────────────────

def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RelayProc:
    """隔离子进程 + 进程树清理（Windows onefile 两层进程；terminate 会漏子进程）。"""

    def __init__(self, *, use_source: bool, data_dir: Path, port: int):
        env = {**os.environ, "RELAY_DATA_DIR": str(data_dir),
               "RELAY_HOST": "127.0.0.1"}
        if use_source:
            cmd = [sys.executable, str(_REPO / "relay" / "run.py"), "run",
                   "--port", str(port)]
        else:
            cmd = [str(_REPO / "relay" / "dist" / "relay-sidecar-onefile.exe"),
                   "run", "--port", str(port)]
        self.proc = subprocess.Popen(
            cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)

    def wait_ready(self, timeout: float = 90.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.proc.stdout.readline()
            if not line:
                if self.proc.poll() is not None:
                    return False
                continue
            if "[relay-ready]" in line:
                return True
        return False

    def kill(self) -> None:
        subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _req(port: int, key: str, path: str, body: dict | None = None,
         timeout: float = 180.0) -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}" + urllib.parse.quote(path, safe="/?=&")
    req = urllib.request.Request(
        url, method="POST" if body else "GET",
        data=json.dumps(body).encode() if body else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


# ─────────────────────────── 用例 ───────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", action="store_true", help="源码态（run.py）替代打包 exe")
    args = ap.parse_args()

    exe = _REPO / "relay" / "dist" / "relay-sidecar-onefile.exe"
    if not args.source and not exe.exists():
        print(f"[skip] 未找到 {exe}（先跑 relay/build_sidecar.py 或加 --source）")
        return 2
    if not KEY_PATH.exists():
        print(f"[skip] 无用户状态 {KEY_PATH}")
        return 2

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, note: str = "") -> None:
        results.append((name, bool(ok), note))

    # 隔离数据目录：复制状态与小号账号（只读源）
    data = Path(tempfile.mkdtemp(prefix="cp-e2e8-"))
    shutil.copy2(KEY_PATH, data / "relay_state.json")
    has_wb = WB_PATH.exists()
    if has_wb:
        shutil.copy2(WB_PATH, data / "platform_workbuddy.json")
    key = json.loads((data / "relay_state.json").read_text("utf-8"))["config"]["api_key"]

    # 假自定义供应商上游
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _FakeUpstream)
    up_port = upstream.server_address[1]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    port = _free_port()
    relay = RelayProc(use_source=args.source, data_dir=data, port=port)
    try:
        check("起服就绪", relay.wait_ready())

        code, body = _req(port, key, "/v1/models?all=1")
        ids = [m["id"] for m in json.loads(body)["data"]] if code == 200 else []
        niu = [i for i in ids if i.startswith("牛码/")]
        wb = [i for i in ids if i.startswith("WorkBuddy/")]
        check(f"三 provider 目录合并（牛码{len(niu)}/WorkBuddy{len(wb)}）",
              bool(niu) and (bool(wb) or not has_wb))

        # 牛码回归（登录态 + 前缀目录 + 单模型端点；不触发对话）
        code, body = _req(port, key, "/v1/models/牛码/glm-5.3-flash")
        try:
            niu_ok = code == 200 and json.loads(body)["id"] == "牛码/glm-5.3-flash"
        except ValueError:
            niu_ok = False
        check("牛码前缀单模型端点", niu_ok)

        # 白名单：写入 WorkBuddy 模型（GUI 供应商页勾选等价操作）
        state = json.loads((data / "relay_state.json").read_text("utf-8"))
        wl = sorted(set(state["config"].get("model_whitelist") or []) | {WB_MODEL})
        code, body = _req(port, key, "/v1/auth/config", {"model_whitelist": wl})
        check("白名单写入（含 WorkBuddy 模型）",
              code == 200 and WB_MODEL in body)

        # 未启用模型 → 404（白名单语义）
        code, body = _req(port, key, "/v1/chat/completions",
                          {"model": "WorkBuddy/auto", "max_tokens": 16,
                           "messages": [{"role": "user", "content": "hi"}]})
        check("未启用模型 404（白名单生效）", code == 404)

        # 未知前缀 → 404
        code, body = _req(port, key, "/v1/chat/completions",
                          {"model": "不存在/glm", "max_tokens": 16,
                           "messages": [{"role": "user", "content": "hi"}]})
        check("未知前缀 404", code == 404)

        if has_wb:
            # WorkBuddy 对话（小号；限次）
            code, body = _req(port, key, "/v1/chat/completions",
                              {"model": WB_MODEL, "stream": True, "max_tokens": 64,
                               "messages": [{"role": "user", "content": "只回复两个字：你好"}]})
            check("WorkBuddy 流式对话", code == 200
                  and '"content": "' in body and "[DONE]" in body, f"code={code}")

            code, body = _req(port, key, "/v1/chat/completions",
                              {"model": WB_MODEL, "max_tokens": 64,
                               "messages": [{"role": "user", "content": "只回复两个字：你好"}]})
            try:
                ok = code == 200 and bool(json.loads(body)["choices"][0]["message"]["content"])
            except (ValueError, KeyError, IndexError):
                ok = False
            check("WorkBuddy 非流式对话（内部聚合）", ok, f"code={code}")

            tools = [{"type": "function", "function": {
                "name": "get_weather", "description": "Get weather",
                "parameters": {"type": "object",
                               "properties": {"city": {"type": "string"}},
                               "required": ["city"]}}}]
            code, body = _req(port, key, "/v1/chat/completions",
                              {"model": WB_MODEL, "max_tokens": 200, "tools": tools,
                               "messages": [{"role": "user",
                                             "content": "Weather in Beijing? "
                                                        "Call get_weather."}]})
            try:
                tcs = json.loads(body)["choices"][0]["message"].get("tool_calls") or []
                ok = code == 200 and tcs and tcs[0]["function"]["name"] == "get_weather"
            except (ValueError, KeyError, IndexError):
                ok = False
            check("WorkBuddy 工具调用透传", ok, f"code={code}")

            # 三协议（A 段：入站层三协议 e2e，上游=WorkBuddy）
            code, body = _req(port, key, "/v1/responses",
                              {"model": WB_MODEL, "stream": True, "input": "只回复两个字：你好"})
            check("responses 协议流式（A 段）", code == 200
                  and "response.output_text.delta" in body, f"code={code}")

            code, body = _req(port, key, "/v1/messages",
                              {"model": WB_MODEL, "max_tokens": 64, "stream": True,
                               "messages": [{"role": "user", "content": "只回复两个字：你好"}]})
            check("messages 协议流式（A 段）", code == 200
                  and "content_block_delta" in body, f"code={code}")

        # 自定义供应商：真转发（假上游）
        code, body = _req(port, key, "/v1/providers/custom", {
            "name": CUSTOM_NAME, "base_url": f"http://127.0.0.1:{up_port}/v1",
            "api_key": "sk-custom-1", "models": [CUSTOM_MODEL]})
        check("创建自定义供应商", code == 200)

        # 启用自定义模型（白名单）
        state = json.loads((data / "relay_state.json").read_text("utf-8"))
        wl = sorted(set(state["config"].get("model_whitelist") or [])
                    | {f"{CUSTOM_NAME}/{CUSTOM_MODEL}"})
        _req(port, key, "/v1/auth/config", {"model_whitelist": wl})

        code, body = _req(port, key, "/v1/chat/completions",
                          {"model": f"{CUSTOM_NAME}/{CUSTOM_MODEL}", "stream": True,
                           "messages": [{"role": "user", "content": "hi"}]})
        check("自定义供应商流式转发", code == 200
              and "来自假上游" in body and "[DONE]" in body, f"code={code}")
        check("自定义：剥前缀传裸名", _FakeUpstream.seen["model"] == CUSTOM_MODEL,
              f"seen={_FakeUpstream.seen['model']}")
        check("自定义：上游鉴权用条目 key",
              _FakeUpstream.seen["auth"] == "Bearer sk-custom-1")

        code, body = _req(port, key, "/v1/responses",
                          {"model": f"{CUSTOM_NAME}/{CUSTOM_MODEL}", "stream": True,
                           "input": "hi"})
        check("自定义供应商 responses 协议", code == 200
              and "response.output_text.delta" in body, f"code={code}")

        code, body = _req(port, key, "/v1/models/" f"{CUSTOM_NAME}/{CUSTOM_MODEL}")
        check("自定义单模型端点", code == 200
              and json.loads(body)["id"] == f"{CUSTOM_NAME}/{CUSTOM_MODEL}")
    finally:
        relay.kill()
        upstream.shutdown()
        shutil.rmtree(data, ignore_errors=True)

    # ── WorkBuddy 无账号 503：另起隔离实例（只有牛码状态，无账号池）──
    data2 = Path(tempfile.mkdtemp(prefix="cp-e2e8-noacct-"))
    shutil.copy2(KEY_PATH, data2 / "relay_state.json")
    port2 = _free_port()
    relay2 = RelayProc(use_source=args.source, data_dir=data2, port=port2)
    try:
        check("无账号实例起服", relay2.wait_ready())
        code, body = _req(port2, key, "/v1/chat/completions",
                          {"model": WB_MODEL, "max_tokens": 16,
                           "messages": [{"role": "user", "content": "hi"}]})
        check("WorkBuddy 无账号 503", code == 503, f"code={code}")
    finally:
        relay2.kill()
        shutil.rmtree(data2, ignore_errors=True)

    # ── 汇总 ──
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, note in results:
        line = ("PASS " if ok else "FAIL ") + name
        if note:
            line += f"  [{note}]"
        print(line)
    print(f"== {passed}/{len(results)} 通过 ==" if passed == len(results)
          else f"== {passed}/{len(results)} 通过（有失败） ==")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
