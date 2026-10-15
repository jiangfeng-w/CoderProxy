#!/usr/bin/env python
"""【已封存·禁止运行 2026-10-16】思考强度透传档位 §5-6 探针：deepseek 系「enabled 无档位」上游行为观察（固定 2 请求）。

背景（spec 思考强度透传档位 §2.2）：形状归一透传后，agent「只表态开、未给档」
（如 claude 入站 thinking=true 不带 budget / messages 入站仅 thinking 开关）→
出站形状 = `thinking:{type:"enabled"}` + `reasoning_summary:"auto"`，**不带
reasoning_effort**（缺档不兜底，设计如此）。旧版此处会补目录默认档/兜底 high。

判定问题：上游对该形状（enabled + summary 但无档位）实际怎么应答？

- P0 基线：enabled + reasoning_effort:"high"（官方完整形状，已知好）→
  预期 200 且 reasoning_tokens>0（账号/模型/头族 sanity + 思考判定法有效）；
- P1 观察：enabled + reasoning_summary:"auto"，无 reasoning_effort →
  - reasoning_tokens>0：上游按某默认档思考（缺档不阻断思考）；
  - reasoning_tokens=0 / 无 reasoning_content 流：上游判「未开思考」
    （isThinkingEnabled 需要 effort——thinking.go 逆向口径在此上游成立）；
  - 400：档位必填（缺档直接拒）。

判读写回 spec §5-6。安全：只读小号账号池（uid 前缀护栏）；不打印 token；
固定 2 请求，跑完勿重跑。
用法：py -3.13 "docs/spec/思考强度透传档位/探针-wb-thinking-enabled-无档位.py"
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

APPDATA_DATA = Path(os.environ["APPDATA"]) / "com.coderproxy.desktop" / "data"
WB_FILE = APPDATA_DATA / "platform_workbuddy.json"

BASE = "https://copilot.tencent.com"
CHAT_URL = BASE + "/v2/chat/completions"
WEB_ORIGIN = "https://www.codebuddy.cn"
DESKTOP_UA = "WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1"
MODEL_BARE = "deepseek-v4.1-flash"

# 规则 5 硬护栏：只允许小号（账号池首位 normal 且 uid 前缀匹配）。
XIAOHAO_UID_PREFIX = "522666c8"


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


def body(thinking: dict, effort: str | None) -> dict:
    b = {"model": MODEL_BARE, "stream": True, "max_tokens": 96,
         "messages": [{"role": "user", "content": "请只回复：完成"}],
         "thinking": thinking, "reasoning_summary": "auto"}
    if effort:
        b["reasoning_effort"] = effort
    return b


async def call(client: httpx.AsyncClient, acct: dict, thinking: dict,
               effort: str | None) -> dict:
    out = {"status": None, "prompt_tokens": None, "reasoning_tokens": None,
           "content": "", "has_reasoning_delta": False, "error": None}
    async with client.stream("POST", CHAT_URL, headers=wb_headers(acct),
                             json=body(thinking, effort),
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
            if isinstance(usage, dict):
                if usage.get("prompt_tokens") is not None:
                    out["prompt_tokens"] = usage["prompt_tokens"]
                if usage.get("reasoning_tokens") is not None:
                    out["reasoning_tokens"] = usage["reasoning_tokens"]
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if isinstance(d.get("content"), str):
                    out["content"] += d["content"]
                if isinstance(d.get("reasoning_content"), str) and d["reasoning_content"]:
                    out["has_reasoning_delta"] = True
    return out


async def main() -> int:
    acct = load_account()
    cases = [
        ("P0 基线 enabled+high", {"type": "enabled"}, "high"),
        ("P1 观察 enabled 无档位", {"type": "enabled"}, None),
    ]
    results: dict[str, dict] = {}
    async with httpx.AsyncClient(trust_env=False) as client:
        for name, thinking, effort in cases:
            r = await call(client, acct, thinking, effort)
            results[name] = r
            print(f"\n[{name}] status={r['status']} code={r.get('code')} "
                  f"prompt={r['prompt_tokens']} reasoning_tokens={r['reasoning_tokens']} "
                  f"reasoning流={r['has_reasoning_delta']}")
            if r["error"]:
                print(f"   error: {r['error'][:300]}")
            else:
                print(f"   content={r['content'][:80]!r}")
            await asyncio.sleep(1.5)

    p0, p1 = results["P0 基线 enabled+high"], results["P1 观察 enabled 无档位"]
    print("\n== 判读 ==")
    # 思考判定信号：reasoning_content 流为主（2026-10-11 实测该形状 usage 不回报
    # reasoning_tokens 字段——None），reasoning_tokens 作辅助。
    p0_ok = p0["status"] == 200 and (p0["has_reasoning_delta"]
                                     or (p0["reasoning_tokens"] or 0) > 0)
    print(f"P0 基线：{'OK（思考判定法有效）' if p0_ok else '异常——账号/模型/头族问题，P1 结论不可信'}")
    if p1["status"] != 200:
        print(f"P1 = {p1['status']}/{p1.get('code')} → **档位必填**：enabled 无档位上游直接拒"
              "（形状归一需重估：缺档是否要兜底回目录档）")
    elif p1["has_reasoning_delta"] or (p1["reasoning_tokens"] or 0) > 0:
        print("P1 出思考（reasoning 流或 reasoning_tokens>0）→ **上游按默认档思考**："
              "缺档不阻断思考，enabled 无档位形状安全（透传归一现状成立）")
    else:
        print("P1 不出思考（无 reasoning 流、reasoning_tokens 缺省）→ **缺档=不思考**：agent"
          "「只开不给档」在透传下等效不思考（thinking.go isThinkingEnabled 需 effort 口径"
          "成立；summary:auto 单独不亮），如实记录")
    print("（固定 2 请求已用完，勿重跑；上游行为可能随版本变化）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
