#!/usr/bin/env python
"""WorkBuddy 思考字段形状探针（三协议入站 → relay → 真上游，小号只读，最小请求数）。

⚠ 2026-10-16：`--direct` 直连分支**已封存禁用**（违反 AGENTS.md 硬性规则 6）；`--via-relay` 保留可用。

用途：定位「agent 设置的思考强度只有 /v1/responses 生效」类问题（2026-10-09 用户实测），
并作为修复后的回归证据。对每个形状记录：HTTP 状态 / usage.reasoning_tokens /
响应里 reasoning_content 字符数。

用法：
  # 直连上游对照（不经 relay；需要小号账号池里有 normal 号）
  py -3.13 "docs/spec/WorkBuddy聊天反代与多平台聚合/探针-wb思考形状.py" --direct
  # 经 relay（隔离数据目录起源码态 relay，复制用户真实状态只读）
  py -3.13 .../探针-wb思考形状.py --via-relay

安全：不打印 token；账号数据只读复制；请求数最小化（默认 5 个形状 + 可选直连 7 个）。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "relay", _REPO / "relay" / "_deps"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import httpx  # noqa: E402

APPDATA_DATA = Path(os.environ["APPDATA"]) / "com.coderproxy.desktop" / "data"
WB_FILE = APPDATA_DATA / "platform_workbuddy.json"
STATE_FILE = APPDATA_DATA / "relay_state.json"

BASE = "https://copilot.tencent.com"
CHAT_URL = BASE + "/v2/chat/completions"
WEB_ORIGIN = "https://www.codebuddy.cn"
DESKTOP_UA = "WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1"
MODEL_BARE = "deepseek-v4.1-flash"
MODEL_FULL = f"WorkBuddy/{MODEL_BARE}"
PROMPT = "一个水池有进水管和出水管：进水管 3 小时注满，出水管 5 小时放空。两管同开，几小时注满？先推理再只输出数字。"

# 形状清单：(名称, 入站协议, 额外字段)。入站协议决定打到哪个端点。
SHAPES = [
    ("chat: effort=max + thinking.enabled（ZCode chat 实发形状）", "chat",
     {"reasoning_effort": "max", "thinking": {"type": "enabled"}}),
    ("chat: 仅 thinking.enabled（无 effort）", "chat",
     {"thinking": {"type": "enabled"}}),
    ("messages: thinking.enabled（ZCode claude 实发形状，无 budget）", "messages",
     {"thinking": {"type": "enabled"}}),
    ("responses: reasoning.effort=max（ZCode responses 实发形状）", "responses",
     {"reasoning": {"effort": "max"}}),
    ("chat: thinking.disabled（关思考意图）", "chat",
     {"thinking": {"type": "disabled"}}),
]


def stable_id(uid: str, purpose: str) -> str:
    return hashlib.sha256(f"wbcp:{purpose}:{uid}".encode()).hexdigest()[:36]


def load_account() -> dict:
    data = json.loads(WB_FILE.read_text("utf-8"))
    accts = [a for a in (data.get("accounts") or []) if a.get("status") == "normal"]
    if not accts:
        raise SystemExit(f"无 normal 账号（{WB_FILE}）")
    return accts[0]


def wb_headers(acct: dict) -> dict:
    uid = str(acct.get("uid") or "")
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": WEB_ORIGIN,
        "Referer": WEB_ORIGIN + "/",
        "User-Agent": DESKTOP_UA,
        "X-CodeBuddy-Request": "1",
        "Accept-Language": "zh-CN",
        "X-Agent-Purpose": "conversation",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Version": "5.5.4",
        "X-Product": "WorkBuddy",
        "Authorization": "Bearer " + (acct.get("access_token") or ""),
    }
    if uid:
        h["X-User-Id"] = uid
        h["X-Machine-ID"] = stable_id(uid, "machine")
        h["X-Session-ID"] = stable_id(uid, "session")
    dom = acct.get("domain") or ""
    h["X-Domain"] = dom if dom else ""
    if dom:
        pass
    else:
        h["X-No-Department-Info"] = "1"
    if acct.get("enterprise_id"):
        h["X-Enterprise-Id"] = acct["enterprise_id"]
    else:
        h["X-No-Enterprise-Id"] = "1"
    return {k: v for k, v in h.items() if v}


async def _measure(client: httpx.AsyncClient, *, url: str, headers: dict,
                   body: dict, timeout: float = 120.0) -> dict:
    """发一次请求并汇总 reasoning 指标（SSE 或 JSON 都收敛为同口径）。"""
    out = {"status": None, "reason_tokens": None, "reason_chars": 0,
           "content_chars": 0, "error": None}
    try:
        async with client.stream("POST", url, headers=headers, json=body,
                                 timeout=httpx.Timeout(timeout, connect=15.0)) as resp:
            out["status"] = resp.status_code
            if resp.status_code != 200:
                text = (await resp.aread()).decode("utf-8", "replace")
                out["error"] = text[:200]
                return out
            async for line in resp.aiter_lines():
                text = line.strip()
                if not text or text.startswith(":"):
                    continue
                if text.startswith("data:"):
                    payload = text[5:].strip()
                    if not payload or payload == "[DONE]":
                        continue
                    try:
                        obj = json.loads(payload)
                    except ValueError:
                        continue
                else:
                    # 非流式 JSON 体（relay 聚合返回）
                    try:
                        obj = json.loads(text)
                    except ValueError:
                        continue
                out["_saw_json"] = True
                _absorb(obj, out)
    except httpx.HTTPError as exc:
        out["error"] = f"HTTPError: {exc}"
    return out


def _absorb(obj: dict, out: dict) -> None:
    """从上游帧/聚合 JSON 里吸收 reasoning 指标（三协议形态兼容）。

    - chat：choices[].delta|message.{content, reasoning_content} + usage
    - anthropic（relay 回译）：content_block_delta.delta.{type:text_delta|thinking_delta,
      text|thinking} + message_delta.usage.output_tokens（无 reasoning 明细）
    - responses（relay 回译）：response.reasoning_summary_text.delta.delta（文本）+
      response.completed.response.usage.output_tokens_details.reasoning_tokens
    """
    etype = obj.get("type")
    # — responses 事件族 —
    if etype == "response.reasoning_summary_text.delta":
        if isinstance(obj.get("delta"), str):
            out["reason_chars"] += len(obj["delta"])
        return
    if etype == "response.completed":
        resp = obj.get("response") or {}
        usage = resp.get("usage") or {}
        det = usage.get("output_tokens_details") or {}
        if det.get("reasoning_tokens") is not None:
            out["reason_tokens"] = det["reasoning_tokens"]
        for blk in resp.get("output") or []:
            if isinstance(blk, dict) and blk.get("type") == "reasoning":
                for s in blk.get("summary") or []:
                    if isinstance(s, dict) and s.get("text"):
                        out["reason_chars"] += len(s["text"])
        return
    # — anthropic 事件族 —
    if etype == "content_block_delta":
        delta = obj.get("delta") or {}
        dt = delta.get("type")
        if dt == "thinking_delta" and isinstance(delta.get("thinking"), str):
            out["reason_chars"] += len(delta["thinking"])
        if dt == "text_delta" and isinstance(delta.get("text"), str):
            out["content_chars"] += len(delta["text"])
        return
    if etype == "message_delta":
        usage = obj.get("usage") or {}
        if usage.get("output_tokens") is not None:
            out["completion_tokens"] = usage["output_tokens"]
        return
    # — chat 帧 / 聚合 JSON —
    usage = obj.get("usage")
    if isinstance(usage, dict):
        det = usage.get("completion_tokens_details") or {}
        if det.get("reasoning_tokens") is not None:
            out["reason_tokens"] = det["reasoning_tokens"]
        if usage.get("completion_tokens") is not None:
            out["completion_tokens"] = usage["completion_tokens"]
    for ch in obj.get("choices") or []:
        if not isinstance(ch, dict):
            continue
        for src in (ch.get("delta") or {}, ch.get("message") or {}):
            if isinstance(src.get("reasoning_content"), str):
                out["reason_chars"] += len(src["reasoning_content"])
            if isinstance(src.get("content"), str):
                out["content_chars"] += len(src["content"])
    # responses 非流式
    for blk in obj.get("output") or []:
        if isinstance(blk, dict) and blk.get("type") == "reasoning":
            for s in blk.get("summary") or []:
                if isinstance(s, dict) and s.get("text"):
                    out["reason_chars"] += len(s["text"])


# ─────────────────────────── 直连模式 ───────────────────────────

async def run_direct() -> int:
    acct = load_account()
    # ── 【封存护栏 2026-10-16】--direct 绕过伪装层直连上游（违反 AGENTS.md 硬性规则 6）──
    # 以下直连逻辑保留仅作历史记录，永不执行（护栏先行 raise）。
    raise SystemExit(
        "[封存禁用 2026-10-16] --direct 直连分支已禁用（AGENTS.md 硬性规则 6）。\n"
        "请使用 --via-relay（经本地 relay 回放）；事故说明见 docs/上游风控与客户端兼容.md §5。")
    results = []
    async with httpx.AsyncClient(trust_env=False) as client:
        for name, _proto, extra in SHAPES:
            body = {"model": MODEL_BARE, "stream": True,
                    "messages": [{"role": "user", "content": PROMPT}], **extra}
            r = await _measure(client, url=CHAT_URL, headers=wb_headers(acct), body=body)
            results.append((name, r))
            print(f"  {name:<64} reason_tok={r['reason_tokens']} "
                  f"reason_chars={r['reason_chars']} status={r['status']}")
            await asyncio.sleep(1.5)
    print("\n== 直连结论 ==")
    for name, r in results:
        mark = "思考✓" if (r["reason_tokens"] or r["reason_chars"]) else "思考✗"
        print(f"  {mark}  {name}")
    return 0


# ─────────────────────────── 经 relay 模式 ───────────────────────────

class _Relay:
    def __init__(self, data_dir: Path, port: int):
        env = {**os.environ, "RELAY_DATA_DIR": str(data_dir), "RELAY_HOST": "127.0.0.1"}
        self.proc = subprocess.Popen(
            [sys.executable, str(_REPO / "relay" / "run.py"), "run", "--port", str(port)],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
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
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _body_for(proto: str, extra: dict) -> dict:
    if proto == "chat":
        return {"model": MODEL_FULL, "stream": True, "max_tokens": 512,
                "messages": [{"role": "user", "content": PROMPT}], **extra}
    if proto == "messages":
        return {"model": MODEL_FULL, "stream": True, "max_tokens": 512,
                "messages": [{"role": "user", "content": PROMPT}], **extra}
    return {"model": MODEL_FULL, "stream": True, "max_output_tokens": 512,
            "input": PROMPT, **extra}


async def run_via_relay() -> int:
    data = Path(tempfile.mkdtemp(prefix="cp-think-"))
    shutil.copy2(WB_FILE, data / "platform_workbuddy.json")
    key = ""
    if STATE_FILE.exists():
        shutil.copy2(STATE_FILE, data / "relay_state.json")
        key = json.loads(STATE_FILE.read_text("utf-8"))["config"]["api_key"]
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    relay = _Relay(data, port)
    results = []
    try:
        if not relay.wait_ready():
            print("[via-relay] relay 起服失败")
            return 2
        base = f"http://127.0.0.1:{port}"
        headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
        async with httpx.AsyncClient(trust_env=False) as client:
            for name, proto, extra in SHAPES:
                path = {"chat": "/v1/chat/completions", "messages": "/v1/messages",
                        "responses": "/v1/responses"}[proto]
                r = await _measure(client, url=base + path, headers=headers,
                                   body=_body_for(proto, extra))
                results.append((name, r))
                print(f"  {name:<64} reason_tok={r['reason_tokens']} "
                      f"reason_chars={r['reason_chars']} status={r['status']} err={r['error']}")
                await asyncio.sleep(1.5)
    finally:
        relay.kill()
        shutil.rmtree(data, ignore_errors=True)
    print("\n== 经 relay 结论 ==")
    for name, r in results:
        mark = "思考✓" if (r["reason_tokens"] or r["reason_chars"]) else "思考✗"
        print(f"  {mark}  {name}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--direct", action="store_true",
                    help="【已封存禁用 2026-10-16】直连上游形状对照（违反硬性规则 6，禁止使用）")
    ap.add_argument("--via-relay", action="store_true", help="经 relay 三协议对照（默认）")
    args = ap.parse_args()
    if args.direct:
        # ── 【封存护栏 2026-10-16】--direct 绕过伪装层直连上游（违反 AGENTS.md 硬性规则 6）──
        print("[封存禁用 2026-10-16] --direct 直连分支已禁用（AGENTS.md 硬性规则 6）。\n"
              "请使用 --via-relay（经本地 relay 回放）；事故说明见 "
              "docs/上游风控与客户端兼容.md §5。")
        return 2
    return asyncio.run(run_via_relay())


if __name__ == "__main__":
    raise SystemExit(main())
