#!/usr/bin/env python
"""WorkBuddy reasoning_content 回放实验（直连上游，小号只读，固定 4 请求）。

判定问题（2026-10-10 挂起项）：thinking 模式下历史 assistant 的
``reasoning_content`` 文本，上游到底是「真读进上下文」还是「白名单反序列化
静默丢弃」——决定 relay 的字段级回填（升级真值缓存）有没有质量意义，
还是仅用于过 400 校验。

设计（直连 copilot.tencent.com，不经 relay——避免 relay 回填逻辑污染实验）：

- P1 基线：历史 assistant ``rc=" "``（1 空格最小占位，过 len>0 校验）；
- P2 实验：同位置 ``rc=MARKER``（~1100 字符唯一标记）；
- P3 对照：``rc=" "``、``content`` 追加同一 MARKER——证明测量口径有效
  （content 必被计数；若 P3 无增量则测量法本身失效，结论作废）；
- 判读：Δcontent = P3.prompt_tokens − P1.prompt_tokens；
        Δrc      = P2.prompt_tokens − P1.prompt_tokens；
    Δcontent 显著(≥100) 且 Δrc≈0   → **rc 被静默丢弃**（字段回填仅过校验，
                                     模型收不到 → 真值缓存升级无质量意义）；
    Δrc ≈ Δcontent（≥50%）          → rc 真进上下文（升级有质量意义，可立项）；
    其余                             → 非典型，人工判读（可追加工具回合变体）。
- P4 行为层交叉印证：rc 内嵌唯一暗号，追问模型复述（复述=模型可见）。

安全：只读小号账号池；不打印账号 token；固定 4 请求，跑完别重跑。
用法：py -3.13 "docs/spec/WorkBuddy聊天反代与多平台聚合/探针-wb-reasoning回放.py"
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

MARKER = "axq-probe-mark-7731 " * 60   # ~1140 字符（英文短词，token 密度稳定）
CODEWORD = "紫色大象-7731"


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
    if not dom:
        h["X-No-Department-Info"] = "1"
    if acct.get("enterprise_id"):
        h["X-Enterprise-Id"] = acct["enterprise_id"]
    else:
        h["X-No-Enterprise-Id"] = "1"
    return {k: v for k, v in h.items() if v}


async def call(client: httpx.AsyncClient, acct: dict, messages: list,
               max_tokens: int = 96) -> dict:
    """发一次 thinking 模式请求并汇总 usage / 文本（SSE 收敛）。"""
    body = {"model": MODEL_BARE, "stream": True, "max_tokens": max_tokens,
            "messages": messages,
            "thinking": {"type": "enabled"}, "reasoning_effort": "max"}
    out = {"status": None, "prompt_tokens": None, "cached_tokens": None,
           "completion_tokens": None, "reasoning_tokens": None,
           "content": "", "reason_chars": 0, "error": None}
    async with client.stream("POST", CHAT_URL, headers=wb_headers(acct), json=body,
                             timeout=httpx.Timeout(120.0, connect=15.0)) as resp:
        out["status"] = resp.status_code
        if resp.status_code != 200:
            out["error"] = (await resp.aread()).decode("utf-8", "replace")[:400]
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
            usage = obj.get("usage")
            if isinstance(usage, dict):
                for key in ("prompt_tokens", "completion_tokens"):
                    if usage.get(key) is not None:
                        out[key] = usage[key]
                pdet = usage.get("prompt_tokens_details") or {}
                if pdet.get("cached_tokens") is not None:
                    out["cached_tokens"] = pdet["cached_tokens"]
                cdet = usage.get("completion_tokens_details") or {}
                if cdet.get("reasoning_tokens") is not None:
                    out["reasoning_tokens"] = cdet["reasoning_tokens"]
            for ch in obj.get("choices") or []:
                d = ch.get("delta") or {}
                if isinstance(d.get("content"), str):
                    out["content"] += d["content"]
                if isinstance(d.get("reasoning_content"), str):
                    out["reason_chars"] += len(d["reasoning_content"])
    return out


def convo(content: str, rc: str, ask: str) -> list:
    """三段对话：user 开场 / 待测 assistant（rc 变体） / user 追问。"""
    return [
        {"role": "user", "content": "开始"},
        {"role": "assistant", "content": content, "reasoning_content": rc},
        {"role": "user", "content": ask},
    ]


async def main() -> int:
    acct = load_account()
    print(f"[probe] uid={str(acct.get('uid'))[:8]}… model={MODEL_BARE} "
          f"marker={len(MARKER)}字符 codeword={CODEWORD}")
    cases = [
        ("P1 基线 rc=空格", convo("好的", " ", "请只回复：完成")),
        ("P2 rc=标记", convo("好的", MARKER, "请只回复：完成")),
        ("P3 对照 content+标记", convo("好的" + MARKER, " ", "请只回复：完成")),
        ("P4 暗号在 rc", convo(
            "好的", f"我在这里记下一个暗号：{CODEWORD}",
            "你在上一条的思考里埋了一个暗号，请把它按原样回复；没有印象就回复「无」。")),
    ]
    results: dict[str, dict] = {}
    async with httpx.AsyncClient(trust_env=False) as client:
        for name, msgs in cases:
            # P4 需要模型在推理后还有输出预算复述暗号：放大 max_tokens
            cap = 512 if name.startswith("P4") else 96
            r = await call(client, acct, msgs, max_tokens=cap)
            results[name] = r
            print(f"\n[{name}] status={r['status']} prompt={r['prompt_tokens']} "
                  f"completion={r['completion_tokens']} reasoning={r['reasoning_tokens']} "
                  f"cached={r['cached_tokens']}")
            if r["error"]:
                print(f"   error: {r['error']}")
            print(f"   content={r['content'][:100]!r} reason_chars={r['reason_chars']}")
            await asyncio.sleep(1.5)

    p1, p2, p3, p4 = (results[k] for k in
                      ("P1 基线 rc=空格", "P2 rc=标记", "P3 对照 content+标记", "P4 暗号在 rc"))
    print("\n== 定量判读 ==")
    if None not in (p1["prompt_tokens"], p2["prompt_tokens"], p3["prompt_tokens"]):
        d_rc = p2["prompt_tokens"] - p1["prompt_tokens"]
        d_content = p3["prompt_tokens"] - p1["prompt_tokens"]
        print(f"Δcontent(P3−P1) = {d_content}  （对照组：content 标记必被计数，应显著>0）")
        print(f"Δrc(P2−P1)      = {d_rc}  （实验组：rc 标记）")
        if d_content >= 100 and abs(d_rc) <= max(20, int(d_content * 0.1)):
            print("结论：**rc 被静默丢弃** → 字段级回填仅过 400 校验，模型收不到内容"
                  "（真值缓存升级无质量意义）")
        elif d_content >= 100 and d_rc >= int(d_content * 0.5):
            print("结论：rc 真进上下文 → 真值回填有质量意义（可立项升级）")
        else:
            print("结论：非典型结果 → 人工判读（可追加工具回合变体再验）")
    else:
        print("缺 prompt_tokens，无法定量判读（检查上方 error）")
    hit = CODEWORD in p4["content"]
    print(f"\n== 行为判读（P4）== 模型回答={p4['content'][:120]!r} → "
          f"{'复述了暗号（rc 模型可见）' if hit else '未复述暗号（与「静默丢弃」一致）'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
