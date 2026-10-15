#!/usr/bin/env python
"""【已封存·禁止运行 2026-10-16】bug4 实证：responses 并行工具调用 → 出站消息顺序断裂 → 上游 11148（只读小号，默认 2 请求）。

用法：py -3.13 "docs/spec/WorkBuddy聊天反代与多平台聚合/复现-wb工具配对断裂.py" [--case-c]

做两件事：
1. 用 `responses_to_chat_request` 转换 ZCode 昨晚失败请求的等价 input（fc,fc → fco,fco
   → fc → fco），打印产出的 messages 序列；
2. 对真实上游打两组 A/B（+可选 C）：
   A = 转换器当前产出 → 修根前预期 400/11148（实证根因）；**修根后（BUG-002 已修复）
       产出即为正确连续形态、预期 200**——此组现用作修根回归；
   B = 手工正确形态（结果紧跟对应 assistant）→ 预期 200；
   `--case-c` 追加 C = 错位形态（结果存在但未紧邻，asst[c1] | user | tool c1）：
   **2026-10-10 实测 = 400/11148（tool calls and tool results do not match）**
   → BUG-004b「错位收拢」层为必需（非保险）。

安全：只读小号账号池；不打印 token；默认 2 请求，`--case-c` 时 3 请求。
"""
from __future__ import annotations

# ── 【封存护栏 2026-10-16】禁止运行：本脚本绕过伪装层直连上游（违反 AGENTS.md 硬性规则 6）──
raise SystemExit(
    "[封存禁用 2026-10-16] 本脚本直连上游，已被禁止运行（AGENTS.md 硬性规则 6）。\n"
    "需要上游证据：改用「经本地 relay 回放」或复用伪装层出站"
    "（范式见 docs/spec/WorkBuddy账号多方式导入/探针-账号端点契约.py）。\n"
    "事故与封存说明：docs/上游风控与客户端兼容.md §5。"
)

import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "relay", _REPO / "relay" / "_deps"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import httpx  # noqa: E402

from relay import protocol_adapter as pa  # noqa: E402

APPDATA_DATA = Path(os.environ["APPDATA"]) / "com.coderproxy.desktop" / "data"
WB_FILE = APPDATA_DATA / "platform_workbuddy.json"
BASE = "https://copilot.tencent.com"
CHAT_URL = BASE + "/v2/chat/completions"
WEB_ORIGIN = "https://www.codebuddy.cn"
DESKTOP_UA = "WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1"
MODEL = "deepseek-v4.1-flash"

# ZCode 昨晚失败请求里的真实 call_id（32 字符，未触发 64 截断）
C1 = "call_00_qgYNeXoxRnZsbgKKKXF17922"
C2 = "call_01_qzZnp84fmWgVVgxl7bjN4797"
C3 = "call_00_aucxmD6FUsGnPTD29DEU8559"


def stable_id(uid: str, purpose: str) -> str:
    return hashlib.sha256(f"wbcp:{purpose}:{uid}".encode()).hexdigest()[:36]


def load_account() -> dict:
    data = json.loads(WB_FILE.read_text("utf-8"))
    accts = [a for a in (data.get("accounts") or []) if a.get("status") == "normal"]
    if not accts:
        raise SystemExit(f"无 normal 账号（{WB_FILE}）")
    return accts[0]


def headers(acct: dict) -> dict:
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
    if dom:
        h["X-Domain"] = dom
    else:
        h["X-No-Department-Info"] = "1"
    if acct.get("enterprise_id"):
        h["X-Enterprise-Id"] = acct["enterprise_id"]
    else:
        h["X-No-Enterprise-Id"] = "1"
    return h


def zcode_input() -> list:
    """ZCode 昨晚 input 的等价最小复现：fc,fc → fco,fco → fc → fco。"""
    return [
        {"type": "message", "role": "user",
         "content": [{"type": "input_text", "text": "继续"}]},
        {"type": "function_call", "call_id": C1, "name": "Bash",
         "arguments": json.dumps({"command": "ls -la /c/Users/JF/.zcode/"})},
        {"type": "function_call", "call_id": C2, "name": "Bash",
         "arguments": json.dumps({"command": "ls /c/Users/JF/.zcode/cli/"})},
        {"type": "function_call_output", "call_id": C1, "output": "total 61"},
        {"type": "function_call_output", "call_id": C2, "output": "agents artifacts"},
        {"type": "function_call", "call_id": C3, "name": "Bash",
         "arguments": json.dumps({"command": "cat /c/Users/JF/.zcode/cli/config.json"})},
        {"type": "function_call_output", "call_id": C3, "output": "{\"plugins\":{}}"},
    ]


def converter_wire() -> list:
    """当前转换器对 zcode_input() 的真实产出（ChatMessage → 线上 dict）。"""
    req = pa.responses_to_chat_request({"input": zcode_input()})
    wire: list[dict] = []
    for m in req.messages:
        d: dict = {"role": m.role, "content": m.content}
        if m.tool_calls:
            d["tool_calls"] = [
                {"id": tc.get("id"), "type": "function",
                 "function": {"name": tc.get("name"),
                              "arguments": tc.get("arguments") if isinstance(
                                  tc.get("arguments"), str)
                              else json.dumps(tc.get("arguments"), ensure_ascii=False)}}
                for tc in m.tool_calls]
        if m.tool_call_id:
            d["tool_call_id"] = m.tool_call_id
        wire.append(d)
    return wire


def messages_fixed() -> list:
    """正确形态：每条 assistant 的 tool_calls 后紧跟其工具结果（修复目标形态）。"""
    return [
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": C1, "type": "function", "function": {"name": "Bash", "arguments": "{}"}},
            {"id": C2, "type": "function", "function": {"name": "Bash", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": C1, "content": "total 61"},
        {"role": "tool", "tool_call_id": C2, "content": "agents artifacts"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": C3, "type": "function", "function": {"name": "Bash", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": C3, "content": "{}"},
    ]


def messages_misplaced() -> list:
    """错位形态：结果存在、但被 user 消息隔开（未被 cleanup 收拢前的形态）。"""
    return [
        {"role": "user", "content": "继续"},
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": C1, "type": "function", "function": {"name": "Bash", "arguments": "{}"}},
        ]},
        {"role": "user", "content": "（工具跑之前又补了一句）"},
        {"role": "tool", "tool_call_id": C1, "content": "total 61"},
    ]


async def call_upstream(client: httpx.AsyncClient, acct: dict, messages: list) -> dict:
    body = {"model": MODEL, "stream": True, "max_tokens": 256, "messages": messages}
    out: dict = {"status": None, "error": None, "content": ""}
    async with client.stream("POST", CHAT_URL, headers=headers(acct), json=body,
                             timeout=httpx.Timeout(120.0, connect=15.0)) as resp:
        out["status"] = resp.status_code
        if resp.status_code != 200:
            raw = (await resp.aread()).decode("utf-8", "replace")
            out["error"] = raw[:400]
            return out
        async for line in resp.aiter_lines():
            t = line.strip()
            if not t.startswith("data:"):
                continue
            payload = t[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except ValueError:
                continue
            if obj.get("error"):
                out["error"] = str(obj["error"])[:300]
            if obj.get("code") not in (None, 0):
                out["error"] = f"code={obj.get('code')} msg={obj.get('msg')}"
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if isinstance(d.get("content"), str):
                    out["content"] += d["content"]
    return out


async def main() -> int:
    acct = load_account()
    print(f"[replay] uid={str(acct.get('uid'))[:8]}… model={MODEL}")

    wire = converter_wire()
    print("\n[A] 转换器产出序列（修复前）:")
    for i, m in enumerate(wire):
        ids = ",".join((tc.get("id") or "")[-6:] for tc in (m.get("tool_calls") or []))
        tid = (m.get("tool_call_id") or "")[-6:]
        print(f"    [{i}] {m['role']:<10} calls={ids:<14} tool_id={tid}")

    async with httpx.AsyncClient(trust_env=False) as client:
        a = await call_upstream(client, acct, wire)
        print(f"\n[A] 转换器当前产出 → status={a['status']} error={a['error']}")
        await asyncio.sleep(1.5)
        b = await call_upstream(client, acct, messages_fixed())
        print(f"[B] 修复形态 → status={b['status']} error={b['error']} "
              f"content={b['content'][:40]!r}")
        c = None
        if "--case-c" in sys.argv:
            await asyncio.sleep(1.5)
            c = await call_upstream(client, acct, messages_misplaced())
            print(f"[C] 错位形态（结果未紧邻）→ status={c['status']} error={c['error']}")
    ok = a["status"] == 200 and b["status"] == 200
    print("\n== 结论：A（修根后产出）与 B 均 200 → 修根回归通过 ==" if ok
          else "== 结论：与预期不符（A 若 400/11148 = 修根回归失败；B 若 400 = 上游/账号异常）==")
    if c is not None:
        print("[C] 结论：" + ("上游拒绝错位形态 → 收拢层为必需（BUG-004b）"
                              if c["status"] != 200
                              else "上游容忍错位形态 → 收拢层为保险"))
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
