#!/usr/bin/env python
"""WorkBuddy 11155 触发条件判别实验（直连上游，小号只读，固定 6 请求）。

判定问题（2026-10-11 P2 挂起项）：上游 11155「reasoning content from the previous
turn must be passed back in thinking mode」的真实触发条件是什么——两个候选口径：

- OmniRoute 口径（open-sse/utils/strictChatHistory.ts 头注释，对 www.workbuddy.ai 实证，
  同族腾讯网关同错误码）：**声明 `tools` 且消息以 assistant 结尾**即报 11155；
  reasoning 是**红鲱鱼**（补 rc 无效），把 trailing assistant 折叠掉同 payload 就过；
- 我方既有口径（2api-panel issue #165 移植，backfill 的立项依据）：thinking 模式
  历史 assistant 缺 `reasoning_content` → 400。两者可能都真（不同错误），也可能
  我方把「trailing assistant 形状」误归因为「rc 缺失」。

矩阵（全部 thinking 开、直连 copilot.tencent.com）：

- C1 基线：无 tools、user 结尾、历史 assistant 带 rc → 预期 200（账号/模型 sanity）；
- C2 rc 缺失：无 tools、user 结尾、历史 assistant **不带 rc 键** → 2api-panel 口径
  是否在我方上游复现？错误码是不是 11155？（决定 rc 回填是第一手依据还是纯保险）；
- C3 OmniRoute 形状：tools + 以纯 commentary assistant 结尾（**rc 已带**）→ OmniRoute
  预言 11155（红鲱鱼直接证据：rc 带了照样拒）；
- C4 对照：C3 消息 + 尾部 user → 预期 200（tools + 非 assistant 结尾 = 正常）；
- C5 tools 必要性：C3 消息去掉 tools 字段 → 200 = tools 声明必要；非 200 = trailing
  assistant 单独即触发；
- C6 折叠修复：C3 的 trailing commentary 按 OmniRoute `foldTrailingCommentary` 语义
  并入 tool-call assistant（content 合并、位于结果前）→ 预期 200（形状修复直接证据）。

判读见脚本末尾。安全：只读小号账号池；不打印账号 token；固定 6 请求，跑完别重跑。
用法：py -3.13 "docs/spec/WorkBuddy聊天反代与多平台聚合/探针-wb-11155-trailing-assistant.py"
"""
from __future__ import annotations

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

APPDATA_DATA = Path(os.environ["APPDATA"]) / "com.coderproxy.desktop" / "data"
WB_FILE = APPDATA_DATA / "platform_workbuddy.json"

BASE = "https://copilot.tencent.com"
CHAT_URL = BASE + "/v2/chat/completions"
WEB_ORIGIN = "https://www.codebuddy.cn"
DESKTOP_UA = "WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1"
MODEL_BARE = "deepseek-v4.1-flash"

# 规则 5 硬护栏：只允许小号（账号池首位 normal 且 uid 前缀匹配）。
XIAOHAO_UID_PREFIX = "522666c8"

C1 = "call_00_probe11155aaaaaaaaaaaaaaaaaaaaa"[:32]

TOOLS = [{
    "type": "function",
    "function": {
        "name": "get_current_time",
        "description": "获取当前时间",
        "parameters": {"type": "object", "properties": {"tz": {"type": "string"}}, "required": []},
    },
}]


def stable_id(uid: str, purpose: str) -> str:
    return hashlib.sha256(f"wbcp:{purpose}:{uid}".encode()).hexdigest()[:36]


def load_account() -> dict:
    data = json.loads(WB_FILE.read_text("utf-8"))
    accts = [a for a in (data.get("accounts") or []) if a.get("status") == "normal"]
    if not accts:
        raise SystemExit(f"无 normal 账号（{WB_FILE}）")
    acct = accts[0]
    uid = str(acct.get("uid") or "")
    if not uid.startswith(XIAOHAO_UID_PREFIX):
        raise SystemExit(f"账号池首位 uid={uid[:8]}… 不是小号（前缀 {XIAOHAO_UID_PREFIX}），拒绝触发")
    return acct


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
    if not dom:
        h["X-No-Department-Info"] = "1"
    if acct.get("enterprise_id"):
        h["X-Enterprise-Id"] = acct["enterprise_id"]
    else:
        h["X-No-Enterprise-Id"] = "1"
    return {k: v for k, v in h.items() if v}


def body(messages: list, tools: list | None) -> dict:
    b = {"model": MODEL_BARE, "stream": True, "max_tokens": 96,
         "messages": messages,
         "thinking": {"type": "enabled"}, "reasoning_effort": "max"}
    if tools is not None:
        b["tools"] = tools
    return b


async def call(client: httpx.AsyncClient, acct: dict, messages: list,
               tools: list | None) -> dict:
    out = {"status": None, "prompt_tokens": None, "content": "", "error": None}
    async with client.stream("POST", CHAT_URL, headers=wb_headers(acct),
                             json=body(messages, tools),
                             timeout=httpx.Timeout(120.0, connect=15.0)) as resp:
        out["status"] = resp.status_code
        if resp.status_code != 200:
            raw = (await resp.aread()).decode("utf-8", "replace")
            out["error"] = raw[:400]
            try:
                j = json.loads(raw)
                out["code"] = j.get("code")
                out["msg"] = j.get("msg")
            except ValueError:
                pass
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
            if obj.get("code") not in (None, 0):
                out["error"] = f"code={obj.get('code')} msg={obj.get('msg')}"
                out["code"] = obj.get("code")
            usage = obj.get("usage")
            if isinstance(usage, dict) and usage.get("prompt_tokens") is not None:
                out["prompt_tokens"] = usage["prompt_tokens"]
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if isinstance(d.get("content"), str):
                    out["content"] += d["content"]
    return out


def tool_call_asst(content: str = "") -> dict:
    return {"role": "assistant", "content": content,
            "tool_calls": [{"id": C1, "type": "function",
                            "function": {"name": "get_current_time",
                                         "arguments": "{\"tz\":\"Asia/Shanghai\"}"}}],
            "reasoning_content": " "}


TOOL_RESULT = {"role": "tool", "tool_call_id": C1, "content": "2026-10-11 12:00:00"}
COMMENTARY = {"role": "assistant", "content": "现在是中午十二点整。", "reasoning_content": " "}


def c3_msgs() -> list:
    """tools + 以纯 commentary assistant 结尾（rc 已带）。"""
    return [{"role": "user", "content": "请查一下当前时间"},
            tool_call_asst(), TOOL_RESULT, COMMENTARY]


async def main() -> int:
    acct = load_account()
    print(f"[probe] uid={str(acct.get('uid'))[:8]}…（小号）model={MODEL_BARE}")
    cases = [
        ("C1 基线 user结尾带rc", [{"role": "user", "content": "开始"},
                                 {"role": "assistant", "content": "好的", "reasoning_content": " "},
                                 {"role": "user", "content": "请只回复：完成"}], None),
        ("C2 历史assistant缺rc", [{"role": "user", "content": "开始"},
                                 {"role": "assistant", "content": "好的"},
                                 {"role": "user", "content": "请只回复：完成"}], None),
        ("C3 tools+trailing assistant(rc齐)", c3_msgs(), TOOLS),
        ("C4 对照 tools+user结尾", c3_msgs() + [{"role": "user", "content": "请只回复：完成"}], TOOLS),
        ("C5 同C3但无tools字段", c3_msgs(), None),
        ("C6 折叠trailing到tool-call assistant",
         [{"role": "user", "content": "请查一下当前时间"},
          {"role": "assistant", "content": "现在是中午十二点整。",
           "tool_calls": tool_call_asst()["tool_calls"], "reasoning_content": " "},
          TOOL_RESULT], TOOLS),
    ]
    results: dict[str, dict] = {}
    async with httpx.AsyncClient(trust_env=False) as client:
        for name, msgs, tools in cases:
            r = await call(client, acct, msgs, tools)
            results[name] = r
            print(f"\n[{name}] status={r['status']} code={r.get('code')} "
                  f"prompt={r['prompt_tokens']}")
            if r["error"]:
                print(f"   error: {r['error'][:300]}")
            else:
                print(f"   content={r['content'][:80]!r}")
            await asyncio.sleep(1.5)

    c3, c4, c5, c6 = (results[k] for k in
                      ("C3 tools+trailing assistant(rc齐)", "C4 对照 tools+user结尾",
                       "C5 同C3但无tools字段", "C6 折叠trailing到tool-call assistant"))
    print("\n== 判读 ==")
    c3_bad = c3["status"] != 200 or c3.get("code") not in (None, 0)
    c3_code = c3.get("code")
    if c3_bad:
        print(f"C3 复现失败形状（code={c3_code}）——与 OmniRoute 预言一致方向")
        if c4["status"] == 200 and c6["status"] == 200:
            print("结论：**11155 触发 = 声明 tools 且以 assistant 结尾；reasoning 红鲱鱼"
                  "（rc 已带仍拒）；折叠 trailing assistant 即过（C6 实证）**")
            print("      → 修复方向 = relay 增 fold trailing commentary（不动 rc 回填）")
        else:
            print(f"但折叠/对照未全过：C4={c4['status']}/{c4.get('code')} "
                  f"C6={c6['status']}/{c6.get('code')} → 红鲱鱼说法存疑，人工判读")
    else:
        print("C3 未复现 11155 → OmniRoute 口径（www.workbuddy.ai）不适用我方上游"
              "（不同产品/网关版本），需看 C2：")
        c2 = results["C2 历史assistant缺rc"]
        if c2["status"] == 200:
            print("C2 也 200 → 我方上游不查历史 rc（backfill = 纯保险，非第一手依据）")
        else:
            print(f"C2 = {c2['status']}/{c2.get('code')} → 2api-panel rc-missing 口径在我方上游复现"
                  "（backfill 有第一手依据）；11155 另有触发条件，待真实报错样本再查")
    print(f"\nC5 tools 必要性对照：{c5['status']}/{c5.get('code')} → "
          f"{'tools 声明必要（无 tools 则同形状放行）' if c5['status'] == 200 else 'trailing assistant 单独即触发'}")
    print("（固定 6 请求已用完，勿重跑；上游行为可能随版本变化）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
