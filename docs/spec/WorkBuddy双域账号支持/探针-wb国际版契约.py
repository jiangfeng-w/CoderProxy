#!/usr/bin/env python
"""【已封存·禁止运行 2026-10-16】WorkBuddy 国际版契约只读探针（双域账号支持 profile 取值实测校准；小号只读）。

用途：用国际版小号（cockpit-tools 导出 JSON）实测校准 `realms.py` 的 profile 取值：
- P1  base 探测：www.workbuddy.ai 的 /v3/config 与 /v2/plugin/accounts 可达性
- P2  企业目录候选序：/v2/enterprises/personal/models → /console/... 回退
- P3  billing 路径候选序：/billing/meter/... （无 /v2）优先，/v2 回退（get-user-resource）
- P4  头族 A/B：UA 平台段 WorkBuddy vs WorkBuddy AI（11140 风控对照）+ Accept-Language
- P5  chat 一次最小流（选目录里一个模型；非流式 → 预期 11101）
- P6  OAuth platform 参数：workbuddy vs CLI（auth/state 接受度）

安全：只读；不打印任何 token；原始响应只落系统临时目录；请求数最小化；只打小号。
运行：py -3.13 "docs/spec/WorkBuddy双域账号支持/探针-wb国际版契约.py" [导出JSON路径]
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
import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]  # docs/spec/<需求名>/ 下
for _p in (_REPO / "relay", _REPO / "relay" / "_deps"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import httpx  # noqa: E402

GLOBAL_BASE = "https://www.workbuddy.ai"
DEFAULT_JSON = Path(r"D:\Download\codebuddy_accounts_2026-10-14.json")
OUT_DIR = Path(tempfile.gettempdir()) / "wb-intl-probe"

DESKTOP_UA_WB = "WorkBuddy/5.5.4 WorkBuddy/5.5.4 CLI/2.137.1"      # CN 现状平台段
DESKTOP_UA_WBAI = "WorkBuddy/5.5.4 WorkBuddy AI/5.5.4 CLI/2.137.1"  # panel global 口径
ORIGIN_INTL = "https://www.workbuddy.ai"


def stable_id(uid: str, purpose: str) -> str:
    return hashlib.sha256(f"wbcp:{purpose}:{uid}".encode()).hexdigest()[:36]


def load_account(path: Path) -> dict:
    """cockpit-tools 导出：数组，取首条（用户小号）；不打印 token。"""
    doc = json.loads(path.read_text("utf-8"))
    rows = doc if isinstance(doc, list) else (doc.get("accounts") or [doc])
    if not rows:
        raise SystemExit(f"导出文件无账号: {path}")
    account = rows[0]
    print(f"[info] 账号 uid={str(account.get('uid'))[:8] or '?'}… "
          f"domain={account.get('domain')} token={'有' if account.get('access_token') else '无'}")
    return account


def headers(account: dict, *, ua: str, lang: str = "en-US",
            extra: dict | None = None) -> dict:
    uid = str(account.get("uid") or "")
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": ORIGIN_INTL,
        "Referer": ORIGIN_INTL + "/",
        "User-Agent": ua,
        "X-CodeBuddy-Request": "1",
        "Accept-Language": lang,
        "X-Agent-Purpose": "conversation",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Version": "5.5.4",
        "X-Product": "WorkBuddy",
        "Authorization": f"Bearer {account.get('access_token') or ''}",
        "X-No-Enterprise-Id": "1",
        "X-Domain": "www.workbuddy.ai",
    }
    if uid:
        h["X-User-Id"] = uid
        h["X-Machine-ID"] = stable_id(uid, "machine")
        h["X-Session-ID"] = stable_id(uid, "session")
    if extra:
        h.update(extra)
    return h


async def get(client: httpx.AsyncClient, url: str, h: dict) -> tuple[int, str]:
    """返回完整响应文本（完整解析；落盘时再截断）。"""
    try:
        r = await client.get(url, headers=h)
        return r.status_code, r.text
    except httpx.HTTPError as exc:
        return -1, f"{exc.__class__.__name__}: {exc}"


async def post_json(client: httpx.AsyncClient, url: str, h: dict,
                    body: dict) -> tuple[int, str]:
    try:
        r = await client.post(url, headers=h, json=body)
        return r.status_code, r.text
    except httpx.HTTPError as exc:
        return -1, f"{exc.__class__.__name__}: {exc}"


async def stream_chat(client: httpx.AsyncClient, h: dict, model: str,
                      *, system_first: str = "") -> dict:
    """一次最小 chat（强 stream）；只统计帧，不回放内容。

    system_first 非空时前置 system 消息（P5 实测：国际版 /v2 首条非 system →
    400/11128 "first message is not system prompt"）。
    """
    messages: list[dict] = []
    if system_first:
        messages.append({"role": "system", "content": system_first})
    messages.append({"role": "user", "content": "hi"})
    body = {"model": model, "stream": True, "messages": messages, "max_tokens": 16}
    out = {"status": 0, "frames": 0, "content_chars": 0, "error": None,
           "first": ""}
    try:
        async with client.stream("POST", f"{GLOBAL_BASE}/v2/chat/completions",
                                 headers=h, json=body) as resp:
            out["status"] = resp.status_code
            if resp.status_code != 200:
                text = (await resp.aread()).decode("utf-8", "replace")
                out["error"] = text[:300]
                return out
            async for line in resp.aiter_lines():
                text = line.strip()
                if not text.startswith("data:"):
                    continue
                payload = text[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                out["frames"] += 1
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                if obj.get("code") not in (None, 0):
                    out["error"] = f"code={obj.get('code')} msg={obj.get('msg') or obj.get('message')}"
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    if isinstance(d.get("content"), str):
                        out["content_chars"] += len(d["content"])
                    if d.get("reasoning_content"):
                        out["content_chars"] += len(str(d["reasoning_content"]))
                if not out["first"]:
                    out["first"] = payload[:200]
    except httpx.HTTPError as exc:
        out["status"] = -1
        out["error"] = f"{exc.__class__.__name__}: {exc}"
    return out


def _dump(name: str, text: str) -> None:
    """原始响应落盘（截断到 8KB 防膨胀；解析用小段在前，足够复核）。"""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{name}.raw.txt").write_text(text[:8000], "utf-8")


async def main() -> None:
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_JSON
    account = load_account(path)
    results: dict = {}
    async with httpx.AsyncClient(trust_env=False, timeout=60.0,
                                 follow_redirects=True) as client:
        h_intl = headers(account, ua=DESKTOP_UA_WBAI)
        h_cn_ua = headers(account, ua=DESKTOP_UA_WB)

        # P1: base 探测（v3/config、plugin/accounts）
        st, txt = await get(client, f"{GLOBAL_BASE}/v3/config", h_intl)
        results["P1_v3_config"] = {"status": st, "code": None}
        _dump("p1_v3_config", txt)
        try:
            results["P1_v3_config"]["code"] = json.loads(txt).get("code")
            models = (json.loads(txt).get("data") or {}).get("models") or []
            results["P1_v3_config"]["models"] = len(models)
            results["P1_v3_config"]["sample"] = [
                str(m.get("id")) for m in models[:8] if isinstance(m, dict)]
        except ValueError:
            pass

        st, txt = await get(client, f"{GLOBAL_BASE}/v2/plugin/accounts", h_intl)
        results["P1_plugin_accounts"] = {"status": st}
        _dump("p1_plugin_accounts", txt)
        try:
            body = json.loads(txt)
            items = (body.get("data") or {}).get("accounts")
            results["P1_plugin_accounts"]["code"] = body.get("code")
            results["P1_plugin_accounts"]["accounts"] = (
                len(items) if isinstance(items, list) else None)
        except ValueError:
            pass

        # P2: 企业目录候选序
        for tag, p in (("v2_family", "/v2/enterprises/personal/models"),
                       ("console_family", "/console/enterprises/personal/models")):
            st, txt = await get(client, GLOBAL_BASE + p, h_intl)
            entry = {"status": st}
            _dump(f"p2_{tag}", txt)
            try:
                body = json.loads(txt)
                entry["code"] = body.get("code")
                models = (body.get("data") or {}).get("models") or []
                entry["models"] = len(models)
                entry["sample"] = [str(m.get("id")) for m in models[:8]
                                   if isinstance(m, dict)]
            except ValueError:
                pass
            results[f"P2_{tag}"] = entry

        # P3: billing 路径候选序（get-user-resource，空 body）
        for tag, p in (("no_v2", "/billing/meter/get-user-resource"),
                       ("with_v2", "/v2/billing/meter/get-user-resource")):
            st, txt = await post_json(client, GLOBAL_BASE + p, h_intl, {})
            entry = {"status": st}
            _dump(f"p3_{tag}", txt)
            try:
                body = json.loads(txt)
                entry["code"] = body.get("code")
                entry["has_accounts"] = "Accounts" in txt
            except ValueError:
                pass
            results[f"P3_{tag}"] = entry

        # P4: UA 平台段 A/B（v3/config 小请求；11140 风控对照）
        for tag, h in (("wb_ai", h_intl), ("wb_cn", h_cn_ua)):
            st, txt = await get(client, f"{GLOBAL_BASE}/v3/config", h)
            entry = {"status": st}
            try:
                entry["code"] = json.loads(txt).get("code")
            except ValueError:
                pass
            results[f"P4_ua_{tag}"] = entry

        # P5: chat 最小流（目录首选模型；带 system 首条——P5a 无 system 已实证 11128）
        model = None
        sample = (results.get("P1_v3_config") or {}).get("sample") or []
        if sample:
            model = sample[0]
        if model:
            chat = await stream_chat(client, h_intl, model,
                                     system_first="You are a helpful assistant.")
            results["P5_chat_system"] = {**chat, "model": model}
            _dump("p5_chat_system", json.dumps(chat, ensure_ascii=False))
            # P5b: UA 平台段对照（chat 层是否也要 WorkBuddy AI）
            chat_cn_ua = await stream_chat(client, h_cn_ua, model,
                                           system_first="You are a helpful assistant.")
            results["P5b_chat_cn_ua"] = {**chat_cn_ua, "model": model}
        else:
            results["P5_chat_system"] = {"skipped": "无目录模型样本"}

        # P7: 配额 summary 路径（CN 主路径；国际版可用性）
        for tag, p in (("summary", "/v2/billing/meter/get-user-resource-summary"),
                       ("status", "/v2/billing/meter/checkin-activity-status")):
            st, txt = await post_json(client, GLOBAL_BASE + p, h_intl, {})
            entry = {"status": st}
            _dump(f"p7_{tag}", txt)
            try:
                body = json.loads(txt)
                entry["code"] = body.get("code")
            except ValueError:
                pass
            results[f"P7_{tag}"] = entry

        # P6: OAuth platform 参数（auth/state；只建 state 不轮询、不授权）
        for tag, param in (("workbuddy", "workbuddy"), ("cli", "CLI")):
            st, txt = await post_json(
                client, f"{GLOBAL_BASE}/v2/plugin/auth/state?platform={param}",
                headers(account, ua=DESKTOP_UA_WBAI,
                        extra={k: v for k, v in
                               {"Authorization": "", "X-No-Authorization": "true",
                                "X-No-User-Id": "true", "X-No-Enterprise-Id": "true",
                                "X-No-Department-Info": "true"}.items()}),
                {})
            entry = {"status": st}
            _dump(f"p6_{tag}", txt)
            try:
                body = json.loads(txt)
                entry["code"] = body.get("code")
                entry["has_state"] = bool((body.get("data") or {}).get("state"))
            except ValueError:
                pass
            results[f"P6_oauth_{tag}"] = entry

    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"[info] 原始响应落盘: {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
