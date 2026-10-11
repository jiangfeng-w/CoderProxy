#!/usr/bin/env python
"""glm-5.3-flash「关思考」有效性判别实验（直连 WB 上游，小号只读，固定 4 请求）。

背景（2026-10-11 用户实测）：软件里选「关」→ 出站 reasoning_effort:"none"
（非 deepseek 的 B4 关形状），客户端仍看到思考输出。已有线索：

- 思考强度控制与日志 spec（2026-09-06 调研）：智谱官方文档称 GLM-5.3/5.3-FLASH
  **不支持 thinking.type=disabled**（直连报错）；Coding Plan 网关把「关」映射为 low；
  GLM-5.3 **仅认 low/high/max**（WB 目录实证一致）——`none` 不在其档位表；
- B4「none 真关」实证仅在 deepseek-v4.1-flash 上做过（探针-wb思考形状.py），
  glm 系从未实测；B4 同实验里「off」被 deepseek 当未知档仍思考——glm 对 none
  很可能同样按未知档落回默认思考。

矩阵（全部 stream、小号）：

- P0 基线：不带任何思考字段 → 默认是否思考（判定法 sanity + 默认行为记录）；
- P1 当前关形状：reasoning_effort:"none"（relay 现行出站）→ 是否仍思考；
- P2 deepseek 关形状：thinking:{type:"disabled"}（无 effort）→ WB 网关报错还是
  接受/忽略（智谱官方口径：GLM-5.3 不支持，直连报错）；
- P3 缓解候选：reasoning_effort:"minimal" → 是否被接受（调研称仅认 low/high/max）。

判定信号：reasoning 流（delta.reasoning_content 非空）为主（该上游 usage 的
reasoning_tokens 常缺省，见 探针-wb-thinking-enabled-无档位.py P0）；tokens 辅助。
判读写回 思考强度透传档位 spec。安全：小号护栏；固定 4 请求，勿重跑。
用法：py -3.13 "docs/spec/思考强度透传档位/探针-wb-glm53flash-关思考.py"
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
MODEL_BARE = "glm-5.3-flash"

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


def body(extra: dict) -> dict:
    b = {"model": MODEL_BARE, "stream": True, "max_tokens": 96,
         "messages": [{"role": "user", "content": "请只回复：完成"}]}
    b.update(extra)
    return b


async def call(client: httpx.AsyncClient, acct: dict, extra: dict) -> dict:
    out = {"status": None, "prompt_tokens": None, "reasoning_tokens": None,
           "content": "", "has_reasoning_delta": False, "error": None}
    async with client.stream("POST", CHAT_URL, headers=wb_headers(acct),
                             json=body(extra),
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
        ("P0 基线（无思考字段）", {}),
        ("P1 none（现行关形状）", {"reasoning_effort": "none"}),
        ("P2 thinking:disabled", {"thinking": {"type": "disabled"}}),
        ("P3 minimal（缓解候选）", {"reasoning_effort": "minimal"}),
    ]
    results: dict[str, dict] = {}
    async with httpx.AsyncClient(trust_env=False) as client:
        for name, extra in cases:
            r = await call(client, acct, extra)
            results[name] = r
            print(f"\n[{name}] status={r['status']} code={r.get('code')} "
                  f"prompt={r['prompt_tokens']} reasoning_tokens={r['reasoning_tokens']} "
                  f"思考流={r['has_reasoning_delta']}")
            if r["error"]:
                print(f"   error: {r['error'][:300]}")
            else:
                print(f"   content={r['content'][:80]!r}")
            await asyncio.sleep(1.5)

    print("\n== 判读 ==")
    p0, p1, p2, p3 = (results[k] for k in
                      ("P0 基线（无思考字段）", "P1 none（现行关形状）",
                       "P2 thinking:disabled", "P3 minimal（缓解候选）"))
    p0_think = p0["has_reasoning_delta"] or (p0["reasoning_tokens"] or 0) > 0
    print(f"P0 默认{'思考' if p0_think else '不思考'}（判定法{'有效' if p0_think else '存疑——模型默认不思考则后续判读失真'}）")
    for name, r in (("P1 none", p1), ("P2 disabled", p2), ("P3 minimal", p3)):
        if r["status"] != 200:
            print(f"{name} = {r['status']}/{r.get('code')} → 上游拒绝该形状")
        else:
            think = r["has_reasoning_delta"] or (r["reasoning_tokens"] or 0) > 0
            print(f"{name} → {'仍思考（关无效）' if think else '不思考（有效关）'}")
    print("（固定 4 请求已用完，勿重跑；上游行为可能随版本变化）")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
