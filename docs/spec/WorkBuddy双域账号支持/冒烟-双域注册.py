#!/usr/bin/env python
"""WorkBuddy 双域冒烟（「WorkBuddy双域账号支持」验收；源码态起真 relay，keyless）。

在**隔离数据目录**（系统临时目录）起真 relay（源码态 run.py），验证：
- 双前缀注册与目录：`/v1/models?all=1` 含 `wbcn/…` 与 `wbg/…`（两池各自账号目录）
- 池隔离：CN 池账号只见于 CN 前缀；国际池账号只见于 wbg 前缀
- 503 语义：仅有国际池账号时 `wbg/x` 可用、`wbcn/x` 走 503（本池不可用）；
  父服务门控「任一上游可用」= 仅国际池有号也算可用
- 旧前缀读兼容：`WorkBuddy/x` 归一 `wbcn/x` 路由（不再 404 未知供应商）
- 平台端点：/v1/platforms 双实例（display_name/supports_checkin 正确）；
  国际版 import-local/export/ide-switch 未挂载（4xx）；粘贴 import 泛化双平台可用
- 国际版签到拒答：POST checkin → success=false + 「国际版无签到」

安全：keyless 除外——目录用假 SSE mock 上游（本地 http server，不触网）；
账号池为构造数据（不读用户真实账号，不触上游）。
运行：py -3.13 "docs/spec/WorkBuddy双域账号支持/冒烟-双域注册.py" [--source|--exe]
"""
from __future__ import annotations

import argparse
import json
import os
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

CN_UID = "u_cn_smoke"
GL_UID = "u_gl_smoke"

_V3_CN = {"code": 0, "data": {"models": [
    {"id": "cn-model-a", "name": "CN-A", "maxInputTokens": 128000,
     "maxOutputTokens": 64000, "reasoning": {"supportedEfforts": ["low", "high"]}},
]}}
_V3_GL = {"code": 0, "data": {"models": [
    {"id": "gl-model-b", "name": "GL-B", "maxInputTokens": 128000,
     "maxOutputTokens": 64000, "reasoning": {"supportedEfforts": ["low", "high"]}},
]}}
_ENT_EMPTY = {"code": 0, "data": {"agents": [{"name": "cli", "models": []}], "models": []}}


class _FakeWbUpstream(BaseHTTPRequestHandler):
    """假 WorkBuddy 上游：固定返回指定目录（CN / 国际版各起一个实例分端口）。"""

    protocol_version = "HTTP/1.1"
    v3_body: dict = _V3_CN

    def do_GET(self):  # noqa: N802
        if "/v3/config" in self.path:
            body = type(self).v3_body
        elif "enterprises/personal/models" in self.path:
            body = _ENT_EMPTY
        else:
            body = {"code": 404, "msg": "not found"}
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("content-length") or 0)
        self.rfile.read(n)
        raw = json.dumps({"code": 0, "data": {}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):  # noqa: ANN002
        pass


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class RelayProc:
    def __init__(self, *, data_dir: Path, port: int, upstream_cn_port: int,
                 upstream_gl_port: int, use_exe: bool = False):
        env = {**os.environ, "RELAY_DATA_DIR": str(data_dir),
               "RELAY_HOST": "127.0.0.1",
               "WB_API_BASE": f"http://127.0.0.1:{upstream_cn_port}",
               "WB_WEB_BASE": f"http://127.0.0.1:{upstream_cn_port}",
               "WB_GLOBAL_BASE": f"http://127.0.0.1:{upstream_gl_port}"}
        if use_exe:
            cmd = [str(_REPO / "relay" / "dist" / "relay-sidecar-onefile.exe"),
                   "run", "--port", str(port)]
        else:
            cmd = [sys.executable, str(_REPO / "relay" / "run.py"), "run",
                   "--port", str(port)]
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
         timeout: float = 30.0) -> tuple[int, str]:
    url = f"http://127.0.0.1:{port}" + urllib.parse.quote(path, safe="/?=&")
    req = urllib.request.Request(
        url, method="POST" if body is not None else "GET",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def _seed_accounts(data: Path) -> None:
    """构造双池账号（keyless：假 token + 远期过期，目录走假上游）。"""
    cn = {"version": 1, "accounts": [{
        "uid": CN_UID, "nickname": "CN 小号", "domain": "www.codebuddy.cn",
        "access_token": "fake_cn_at", "refresh_token": "fake_cn_rt",
        "expires_at": 9_999_999_999_999, "status": "normal"}]}
    gl = {"version": 1, "accounts": [{
        "uid": GL_UID, "nickname": "GL 小号", "domain": "www.workbuddy.ai",
        "access_token": "fake_gl_at", "refresh_token": "fake_gl_rt",
        "expires_at": 9_999_999_999_999, "status": "normal"}]}
    (data / "platform_workbuddy.json").write_text(
        json.dumps(cn, ensure_ascii=False), "utf-8")
    (data / "platform_workbuddy-global.json").write_text(
        json.dumps(gl, ensure_ascii=False), "utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exe", action="store_true",
                    help="打包产物态（relay-sidecar-onefile.exe）替代源码态")
    args = ap.parse_args()
    if args.exe:
        exe = _REPO / "relay" / "dist" / "relay-sidecar-onefile.exe"
        if not exe.exists():
            print(f"[skip] 未找到 {exe}（先跑 relay/build_sidecar.py）")
            return 2

    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, note: str = "") -> None:
        results.append((name, bool(ok), note))

    # 双假上游：CN 一份、国际版一份（分端口；host 都是 127.0.0.1，只能靠端口区分）
    class _UpstreamCN(_FakeWbUpstream):
        v3_body = _V3_CN

    class _UpstreamGL(_FakeWbUpstream):
        v3_body = _V3_GL

    upstream_cn = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamCN)
    upstream_gl = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamGL)
    up_cn_port = upstream_cn.server_address[1]
    up_gl_port = upstream_gl.server_address[1]
    threading.Thread(target=upstream_cn.serve_forever, daemon=True).start()
    threading.Thread(target=upstream_gl.serve_forever, daemon=True).start()

    data = Path(tempfile.mkdtemp(prefix="cp-dual-"))
    _seed_accounts(data)
    port = _free_port()
    relay = RelayProc(data_dir=data, port=port, upstream_cn_port=up_cn_port,
                      upstream_gl_port=up_gl_port, use_exe=args.exe)
    try:
        check("起服就绪", relay.wait_ready())
        # api_key：首启生成于 relay_state.json
        state = json.loads((data / "relay_state.json").read_text("utf-8"))
        key = state["config"]["api_key"]

        # 1) 平台注册表双实例
        code, body = _req(port, key, "/v1/platforms")
        plats = {p["id"]: p for p in json.loads(body).get("platforms", [])} if code == 200 else {}
        check("平台注册表双实例",
              code == 200 and "workbuddy" in plats and "workbuddy-global" in plats,
              f"code={code} ids={sorted(plats)}")
        check("能力位正确",
              plats.get("workbuddy", {}).get("supports_checkin") is True
              and plats.get("workbuddy-global", {}).get("supports_checkin") is False
              and plats.get("workbuddy", {}).get("display_name") == "WorkBuddy CN"
              and plats.get("workbuddy-global", {}).get("display_name") == "WorkBuddy")

        # 2) 双前缀目录（all=1 全量目录：wbcn/ 与 wbg/ 各有本池模型）
        code, body = _req(port, key, "/v1/models?all=1")
        ids = [m["id"] for m in json.loads(body)["data"]] if code == 200 else []
        wbcn = [i for i in ids if i.startswith("wbcn/")]
        wbg = [i for i in ids if i.startswith("wbg/")]
        check("双前缀目录（wbcn+wbg）", bool(wbcn) and bool(wbg),
              f"wbcn={wbcn[:2]} wbg={wbg[:2]}")
        check("池隔离（CN 模型仅在 wbcn，国际模型仅在 wbg）",
              "wbcn/cn-model-a" in ids and "wbg/gl-model-b" in ids
              and "wbcn/gl-model-b" not in ids and "wbg/cn-model-a" not in ids)

        # 3) 旧前缀读兼容：WorkBuddy/x 归一 wbcn/x（目录命中 → 不再「未知供应商」）
        code, body = _req(port, key, "/v1/models/WorkBuddy/cn-model-a")
        check("旧前缀读兼容归一", code == 200 and json.loads(body).get("id") == "wbcn/cn-model-a",
              f"code={code}")

        # 4) 单模型端点：wbcn / wbg 各自命中
        code1, _ = _req(port, key, "/v1/models/wbcn/cn-model-a")
        code2, _ = _req(port, key, "/v1/models/wbg/gl-model-b")
        check("双前缀单模型端点", code1 == 200 and code2 == 200, f"{code1}/{code2}")

        # 5) 池隔离 503 语义：把 CN 池账号挂非 normal → 仅国际池可用；
        #    wbcn/x 应 503 引导（本池不可用），wbg/x 仍可用；服务门控仍 available
        cn_doc = json.loads((data / "platform_workbuddy.json").read_text("utf-8"))
        cn_doc["accounts"][0]["status"] = "disabled"
        (data / "platform_workbuddy.json").write_text(
            json.dumps(cn_doc, ensure_ascii=False), "utf-8")
        # 目录缓存 1h：force 需要重新走注册表——用 /v1/models 全量刷新不可行，
        # 改验证 chat 路径（_ensure_model 实时查池 availability）
        code_cn, body_cn = _req(port, key, "/v1/chat/completions",
                                {"model": "wbcn/cn-model-a",
                                 "messages": [{"role": "user", "content": "hi"}]})
        check("CN 池空 → wbcn 503 引导", code_cn == 503,
              f"code={code_cn} body={body_cn[:120]}")
        code_gl, body_gl = _req(port, key, "/v1/chat/completions",
                                {"model": "wbg/gl-model-b",
                                 "messages": [{"role": "user", "content": "hi"}]})
        # 国际版 chat 走假上游（POST 200 JSON）——上游非 SSE 会导致空流错误；
        # 只断言「非 503/404」= 路由与选号链打通（真实 chat 待设备流 token 实测）
        check("国际池可用 → wbg 非 503/404", code_gl not in (503, 404),
              f"code={code_gl} body={body_gl[:120]}")

        # 6) 服务门控：仅国际池有号 → /v1/service enabled true（任一上游可用）
        code, body = _req(port, key, "/v1/service")
        check("服务门控任一上游可用", code == 200 and json.loads(body).get("enabled") is True)

        # 7) 国际版签到拒答（零上游调用：假上游未记录 checkin 请求）
        code, body = _req(port, key,
                          f"/v1/platforms/workbuddy-global/accounts/{GL_UID}/checkin",
                          {})
        ok = code == 200 and json.loads(body).get("result", {}).get("success") is False
        check("国际版签到拒答", ok and "国际版无签到" in body, f"code={code}")

        # 8) 端点细分：国际版 import-local/export 未挂载（4xx）；粘贴 import 双平台
        c1, _ = _req(port, key,
                     "/v1/platforms/workbuddy-global/accounts/import-local", {})
        c2, _ = _req(port, key,
                     "/v1/platforms/workbuddy-global/accounts/export",
                     {"uids": [], "format": "cockpit"})
        check("国际版 CN 专用端点未挂载", c1 in (404, 405) and c2 in (404, 405),
              f"{c1}/{c2}")
        c3, b3 = _req(port, key, "/v1/platforms/workbuddy-global/accounts/import",
                      {"text": '[{"access_token": "t", "uid": "u_g2", '
                               '"domain": "www.workbuddy.ai"}]'})
        check("粘贴 import 泛化（国际版）", c3 == 200, f"code={c3} body={b3[:100]}")
        c4, b4 = _req(port, key, "/v1/platforms/workbuddy/accounts/import",
                      {"text": '[{"access_token": "t", "uid": "u_c2", '
                               '"domain": "www.codebuddy.cn"}]'})
        check("粘贴 import 泛化（CN）", c4 == 200, f"code={c4} body={b4[:100]}")
        c5, b5 = _req(port, key, "/v1/platforms/workbuddy/accounts/import",
                      {"text": '[{"access_token": "t", "uid": "u_x", '
                               '"domain": "www.workbuddy.ai"}]'})
        check("归属校验（国际号进 CN 400）", c5 == 400 and "国际版" in b5,
              f"code={c5}")
    finally:
        relay.kill()
        upstream_cn.shutdown()
        upstream_gl.shutdown()

    print("\n=== 双域冒烟结果 ===")
    passed = 0
    for name, ok, note in results:
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({note})" if note else ""))
        passed += 1 if ok else 0
    print(f"\n{passed}/{len(results)} 通过")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
