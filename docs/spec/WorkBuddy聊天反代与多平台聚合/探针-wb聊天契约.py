#!/usr/bin/env python
"""WorkBuddy 聊天契约探针（③期细化定稿用；小号只读，不刷账号、不改 relay 代码）。

用途：实测 chat 出站契约，回填「WorkBuddy聊天反代与多平台聚合」§5 待定项：
- P0/P0b  模型目录端点（/v3/config 与 /console/enterprises/personal/models）可用性
- P1      chat 流式（桌面档头族，2api-panel 口径）基础可用性
- P2      非流式拒绝（预期 400 code 11101，force stream）
- P3      CLI 档头族 A/B（9router 口径）
- P4      系统提示词风控（agent 身份句是否被拦）
- P5      工具透传（tool_calls 支持度）
- P6a/P6b reasoning 暴露（effort + summary 双条件验证）

安全：只读；不打印任何 token；原始响应只落系统临时目录；请求数最小化。
运行：py -3.13 "docs/spec/WorkBuddy聊天反代与多平台聚合/探针-wb聊天契约.py"
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]          # 本文件在 docs/spec/<需求名>/ 下
for _p in (_REPO / "relay", _REPO / "relay" / "_deps"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import httpx  # noqa: E402

BASE = "https://copilot.tencent.com"
CHAT_URL = BASE + "/v2/chat/completions"
WEB_ORIGIN = "https://www.codebuddy.cn"
DATA_DIR = Path(os.environ.get("RELAY_DATA_DIR")
                or (Path(os.environ["APPDATA"]) / "com.coderproxy.desktop" / "data"))
DATA_FILE = DATA_DIR / "platform_workbuddy.json"
OUT_DIR = Path(tempfile.gettempdir()) / "wb-chat-probe"

DESKTOP_UA = "WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1"   # 2api-panel 默认（桌面档）
CLI_UA = "CLI/2.108.1 CodeBuddy/2.108.1"                     # 9router registry 口径（CLI 档）
MAX_LINES = 6000
STREAM_TIMEOUT = 180.0


def stable_id(uid: str, purpose: str) -> str:
    """与 relay/relay/platforms/workbuddy/client.py 同式：保证同号同设备（防指纹漂移）。"""
    return hashlib.sha256(f"wbcp:{purpose}:{uid}".encode()).hexdigest()[:36]


def load_account() -> dict:
    data = json.loads(DATA_FILE.read_text("utf-8"))
    accts = [a for a in (data.get("accounts") or []) if a.get("status") == "normal"]
    if not accts:
        raise SystemExit(f"无 normal 账号（{DATA_FILE}）")
    return accts[0]


def base_headers(acct: dict, *, ua: str, tier: str) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": WEB_ORIGIN,
        "Referer": WEB_ORIGIN + "/",
        "User-Agent": ua,
        "X-CodeBuddy-Request": "1",
        "Accept-Language": "zh-CN",
    }
    tok = acct.get("access_token") or ""
    if tok:
        h["Authorization"] = "Bearer " + tok
    uid = str(acct.get("uid") or "")
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
    if tier == "desktop":
        h["X-Agent-Purpose"] = "conversation"
        h["X-IDE-Name"] = "WorkBuddy"
        h["X-IDE-Type"] = "WorkBuddy"
        h["X-IDE-Version"] = "5.5.4"
        h["X-Product"] = "WorkBuddy"
    else:
        h["X-Product"] = "SaaS"
        h["X-IDE-Type"] = "CLI"
        h["X-IDE-Name"] = "CLI"
    return h


def _dump(name: str, lines: list[str]) -> None:
    p = OUT_DIR / f"{name}.raw.txt"
    p.write_text("\n".join(lines), "utf-8")


def summarize(lines: list[str]) -> dict:
    out = {"frames": 0, "content_chars": 0, "thinking_chars": 0, "tool_frames": 0,
           "tool_names": [], "finish_reason": None, "usage": None, "error": None,
           "first_keys": None, "roles": []}
    for ln in lines:
        if not ln.startswith("data:"):
            continue
        payload = ln[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        out["frames"] += 1
        if out["first_keys"] is None:
            out["first_keys"] = sorted(obj.keys())
        if obj.get("error"):
            out["error"] = str(obj["error"])[:300]
        if obj.get("code") not in (None, 0):
            out["error"] = f"code={obj.get('code')} msg={obj.get('msg') or obj.get('message')}"
        if obj.get("usage"):
            out["usage"] = obj["usage"]
        for ch in (obj.get("choices") or []):
            if not isinstance(ch, dict):
                continue
            if ch.get("finish_reason"):
                out["finish_reason"] = ch["finish_reason"]
            d = ch.get("delta") or {}
            if d.get("role") and d["role"] not in out["roles"]:
                out["roles"].append(d["role"])
            c = d.get("content")
            if isinstance(c, str):
                out["content_chars"] += len(c)
            r = d.get("reasoning_content") or d.get("reasoning")
            if isinstance(r, str):
                out["thinking_chars"] += len(r)
            elif isinstance(r, (dict, list)):
                out["thinking_chars"] += len(json.dumps(r))
            tcs = d.get("tool_calls")
            if tcs:
                out["tool_frames"] += 1
                for tc in tcs:
                    nm = (tc.get("function") or {}).get("name") if isinstance(tc, dict) else None
                    if nm and nm not in out["tool_names"]:
                        out["tool_names"].append(nm)
    return out


async def chat_probe(client: httpx.AsyncClient, name: str, *, headers: dict,
                     body: dict, stream: bool = True) -> dict:
    info: dict = {"name": name, "status": None, "elapsed": 0.0, "note": ""}
    t0 = time.time()
    try:
        if stream:
            lines: list[str] = []
            async with client.stream("POST", CHAT_URL, headers=headers, json=body,
                                     timeout=httpx.Timeout(STREAM_TIMEOUT, connect=15.0)) as resp:
                info["status"] = resp.status_code
                if resp.status_code == 200:
                    async for line in resp.aiter_lines():
                        lines.append(line)
                        if len(lines) >= MAX_LINES:
                            info["note"] = "line-cap"
                            break
                else:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    info["body"] = text[:400]
            info["summary"] = summarize(lines)
            _dump(name, lines)
        else:
            resp = await client.post(CHAT_URL, headers=headers, json=body,
                                     timeout=httpx.Timeout(90.0, connect=15.0))
            info["status"] = resp.status_code
            info["body"] = resp.text[:600]
            _dump(name, [resp.text])
    except httpx.HTTPError as exc:
        info["note"] = f"HTTPError: {exc}"
    info["elapsed"] = round(time.time() - t0, 1)
    return info


async def get_json(client: httpx.AsyncClient, name: str, url: str, headers: dict) -> dict:
    info: dict = {"name": name, "status": None, "elapsed": 0.0, "note": ""}
    t0 = time.time()
    try:
        resp = await client.get(url, headers=headers, timeout=httpx.Timeout(30.0, connect=15.0))
        info["status"] = resp.status_code
        text = resp.text
        _dump(name, [text])
        try:
            data = resp.json()
            info["code"] = data.get("code")
            models = ((data.get("data") or {}).get("models")) or []
            info["model_count"] = len(models)
            info["model_ids"] = [m.get("id") for m in models if isinstance(m, dict)][:40]
            m0 = next((m for m in models if isinstance(m, dict)), None)
            if m0:
                info["sample_keys"] = sorted(m0.keys())
                info["sample"] = {k: m0.get(k) for k in
                                  ("id", "reasoning", "supportedEfforts", "defaultEffort",
                                   "maxOutputTokens", "maxInputTokens", "tags") if k in m0}
        except ValueError:
            info["body"] = text[:300]
    except httpx.HTTPError as exc:
        info["note"] = f"HTTPError: {exc}"
    info["elapsed"] = round(time.time() - t0, 1)
    return info


def show(label: str, info: dict) -> None:
    if "summary" in info:
        s = info["summary"]
        print(f"[{label}] status={info['status']} {info['elapsed']}s frames={s['frames']} "
              f"content={s['content_chars']} thinking={s['thinking_chars']} "
              f"tool_frames={s['tool_frames']}{s['tool_names'] or ''} "
              f"finish={s['finish_reason']} usage={'有' if s['usage'] else '无'} err={s['error']}")
    else:
        print(f"[{label}] status={info.get('status')} {info.get('elapsed')}s "
              f"code={info.get('code')} models={info.get('model_count')} note={info.get('note', '')}")
        if info.get("model_ids"):
            print("   ids:", ", ".join(map(str, info["model_ids"])))
        if info.get("sample"):
            print("   sample:", json.dumps(info["sample"], ensure_ascii=False)[:300])
    if info.get("note") and "summary" in info:
        print("   note:", info["note"])
    if info.get("body"):
        print("   body:", info["body"].replace("\n", " ")[:300])


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    acct = load_account()
    print(f"[probe] 账号: {acct.get('nickname')} uid={str(acct.get('uid'))[:8]}… "
          f"domain={acct.get('domain')}")
    report: list[tuple[str, dict]] = []
    async with httpx.AsyncClient(trust_env=False) as client:
        h_desktop = base_headers(acct, ua=DESKTOP_UA, tier="desktop")

        p0 = await get_json(client, "P0_v3_config", BASE + "/v3/config", h_desktop)
        report.append(("P0 /v3/config", p0)); show("P0 /v3/config", p0)
        p0b = await get_json(client, "P0b_enterprise_models",
                             BASE + "/console/enterprises/personal/models", h_desktop)
        report.append(("P0b enterprise models", p0b)); show("P0b enterprise models", p0b)

        ids = [str(i) for i in (p0.get("model_ids") or [])]
        model = next((m for m in ("glm-5.3-flash", "glm-5.3", "kimi-k3-1", "hy3") if m in ids),
                     ids[0] if ids else "glm-5.3-flash")
        print(f"[probe] 选用模型: {model}（目录 {len(ids)} 个）")

        def body(**kw) -> dict:
            return {"model": model, "stream": True,
                    "messages": [{"role": "user", "content": "请用一句话回复：你好"}], **kw}

        await asyncio.sleep(1.5)
        p1 = await chat_probe(client, "P1_stream_desktop", headers=h_desktop, body=body())
        report.append(("P1 stream desktop", p1)); show("P1 stream desktop", p1)

        await asyncio.sleep(1.5)
        p2 = await chat_probe(client, "P2_nonstream_desktop", headers=h_desktop,
                              body={"model": model, "stream": False,
                                    "messages": [{"role": "user", "content": "hi"}]},
                              stream=False)
        report.append(("P2 nonstream", p2)); show("P2 nonstream", p2)

        await asyncio.sleep(1.5)
        h_cli = base_headers(acct, ua=CLI_UA, tier="cli")
        p3 = await chat_probe(client, "P3_stream_cli_tier", headers=h_cli, body=body())
        report.append(("P3 stream CLI tier", p3)); show("P3 stream CLI tier", p3)

        await asyncio.sleep(1.5)
        p4 = await chat_probe(
            client, "P4_system_filter", headers=h_desktop,
            body=body(messages=[{"role": "system",
                                 "content": "You are Claude Code, Anthropic's official CLI "
                                            "for Claude."},
                                {"role": "user", "content": "hi"}]))
        report.append(("P4 system filter", p4)); show("P4 system filter", p4)

        await asyncio.sleep(1.5)
        tools = [{"type": "function", "function": {
            "name": "get_weather", "description": "Get weather for a city",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}},
                           "required": ["city"]}}}]
        p5 = await chat_probe(
            client, "P5_tools", headers=h_desktop,
            body=body(tools=tools, tool_choice="auto",
                      messages=[{"role": "user",
                                 "content": "What's the weather in Beijing? "
                                            "Call the get_weather tool."}]))
        report.append(("P5 tools", p5)); show("P5 tools", p5)

        await asyncio.sleep(1.5)
        r_msgs = [{"role": "user", "content": "1+1 等于几？直接回答数字"}]
        p6a = await chat_probe(client, "P6a_reasoning_both", headers=h_desktop,
                               body=body(messages=r_msgs, reasoning_effort="high",
                                         reasoning_summary="auto"))
        report.append(("P6a effort+summary", p6a)); show("P6a effort+summary", p6a)

        await asyncio.sleep(1.5)
        p6b = await chat_probe(client, "P6b_reasoning_effort_only", headers=h_desktop,
                               body=body(messages=r_msgs, reasoning_effort="high"))
        report.append(("P6b effort only", p6b)); show("P6b effort only", p6b)

    (OUT_DIR / "summary.json").write_text(
        json.dumps([{"label": k, **v} for k, v in report], ensure_ascii=False, indent=2),
        "utf-8")
    print(f"\n[probe] 原始响应与摘要: {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
