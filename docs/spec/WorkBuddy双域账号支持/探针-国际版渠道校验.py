#!/usr/bin/env python
"""WorkBuddy 国际版「渠道校验」受控探针（2026-10-17 新增；**经伪装层出站**、小号限定）。

背景：2026-10-15/16 起国际版（WorkBuddy AI 通道）收紧「渠道校验」——请求缺会话头族
（`X-Conversation-ID` / `X-Conversation-Request-ID` / `X-Conversation-Message-ID`）时
返回 `400 code=11128`（上游 msg `conversation id missing` / 变体 `request illegal`，
displayTips「这次调用没通过渠道校验，可能不是官方客户端发出的」）。两个参考实现已修：
panel PR #190（always send the conversation channel header trio，5 个国际账号实测）、
9router PR #4949（抓包 5.8.4：官方客户端发 5 个 ID 头 + trace 族）。

本探针受控对比 4 种请求形态，定位触发条件并验证修复方向（每形态 1 次请求）：
  1. plain              —— 纯对话（无工具）
  2. tools_declared     —— 带工具声明（无工具历史）
  3. tool_continuation  —— 带 assistant.tool_calls + tool 结果的续轮（复刻本机日志里的失败形态）
  4. fix_conv_headers   —— 同 3，但注入会话头族（panel 口径：32 hex × 3）

安全纪律（AGENTS.md 硬性规则 5/6）：经伪装层出站（复用 adapter `_stream_once` 头族）；
只用小号（--uid 显式指定，不自动挑号）；请求数最小化（4 次）；不打印 token。

运行（默认 dry-run；确需实测加 --run）：
    py -3.13 "docs/spec/WorkBuddy双域账号支持/探针-国际版渠道校验.py" --uid <小号uid> --run
"""
from __future__ import annotations

import argparse
import asyncio
import secrets
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]  # docs/spec/<需求名>/ 下
for _p in (_REPO / "relay", _REPO / "relay" / "_deps"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from relay.platforms import store  # noqa: E402
from relay.platforms.base import PlatformUpstreamError  # noqa: E402
from relay.platforms.workbuddy import chat as wbchat  # noqa: E402
from relay.platforms.workbuddy.realms import REALM_GLOBAL  # noqa: E402

TOOLS = [{
    "type": "function",
    "function": {
        "name": "Bash",
        "description": "Run a shell command",
        "parameters": {"type": "object",
                       "properties": {"command": {"type": "string"}},
                       "required": ["command"]},
    },
}]

_TOOL_CALL = {"id": "call_probe_1", "type": "function",
              "function": {"name": "Bash", "arguments": '{"command": "echo hi"}'}}


def _base_messages() -> list:
    return [{"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Reply with the single word OK."}]


def _continuation_messages() -> list:
    return [{"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Run the shell command: echo hi"},
            {"role": "assistant", "content": None, "tool_calls": [_TOOL_CALL]},
            {"role": "tool", "tool_call_id": "call_probe_1", "content": "hi"},
            {"role": "user", "content": "Reply with the single word OK."}]


def _conversation_trio() -> dict:
    """panel 口径会话头族（PR #190）：三个 32 hex，每请求现生成。"""
    return {
        "X-Conversation-ID": secrets.token_hex(16),
        "X-Conversation-Request-ID": secrets.token_hex(16),
        "X-Conversation-Message-ID": secrets.token_hex(16),
    }


async def _probe_once(adapter, account: dict, body: dict) -> dict:
    frames = 0
    try:
        async for _frame in adapter._stream_once(account, body):
            frames += 1
            if frames >= 3:
                break
        return {"verdict": "pass", "detail": f"正常流（已读 {frames} 帧）"}
    except PlatformUpstreamError as exc:
        text = str(exc)
        code = None
        for token in ("11128", "11140", "11101", "11102", "11133", "11148"):
            if token in text:
                code = token
                break
        return {"verdict": "blocked" if code in ("11128", "11140") else "error",
                "detail": text[:220], "code": code}


async def main(args: argparse.Namespace) -> int:
    platform = REALM_GLOBAL.platform_id
    account = await store.find_account(platform, args.uid)
    if account is None:
        print(f"[probe] 池内找不到该 uid（platform={platform}）")
        return 2
    print(f"[probe] 账号 uid={str(args.uid)[:8]}…  platform={platform}  model={args.model}")
    cases = [
        ("plain", "纯对话（无工具）", _base_messages(), False, False),
        ("tools_declared", "带工具声明（无工具历史）", _base_messages(), True, False),
        ("tool_continuation", "带工具历史续轮（复刻失败形态）", _continuation_messages(), True, False),
        ("fix_conv_headers", "同 3 + 会话头族（panel 口径）", _continuation_messages(), True, True),
    ]
    if not args.run:
        print("[probe] dry-run 计划（4 次最小流式请求，经伪装层）：")
        for key, label, _msgs, _tools, _fix in cases:
            print(f"[probe]   {key:<20} {label}")
        print("[probe] 确认是小号后加 --run 执行")
        return 0

    adapter = wbchat.WorkBuddyChatAdapter(args.model, profile=REALM_GLOBAL)
    account = await adapter._account_adapter.ensure_token(account)
    orig_headers = adapter._headers

    n = 0
    for key, label, messages, tools, fix in cases:
        if fix:
            def _patched(account, _orig=orig_headers):
                headers = dict(_orig(account))
                headers.update(_conversation_trio())
                return headers
            adapter._headers = _patched
        else:
            adapter._headers = orig_headers
        body = {"model": args.model, "stream": True, "messages": messages}
        if tools:
            body["tools"] = TOOLS
        res = await _probe_once(adapter, account, body)
        n += 1
        print(f"[probe] {key:<20} {res['verdict']:<8} {res['detail']}")
    adapter._headers = orig_headers
    print(f"[probe] 完成（总请求 {n} 次）。判读：只有 tool_continuation 挂 → 工具历史形态触发；"
          "fix_conv_headers 转好 → 会话头族即修复方向；全挂 → 通道级校验（需补头族）；全好 → 抽样/边缘节点差异")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="国际版渠道校验受控探针（经伪装层、小号限定）")
    parser.add_argument("--uid", required=True, help="国际版小号 uid（脚本不自动挑号）")
    parser.add_argument("--model", default="deepseek-v4.1-flash",
                        help="探测模型（默认 deepseek-v4.1-flash——本机日志失败模型）")
    parser.add_argument("--run", action="store_true", help="真正发请求（缺省只 dry-run）")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
