#!/usr/bin/env python
"""WorkBuddy 国际版 chat 出站形状定向探针（最小请求数；小号只读）。

背景（前一轮 探针-wb国际版契约.py 实测）：
- 国际版 /v3/config 200；/v2/enterprises/personal/models 200（/console 500）→ /v2 家族优先
- billing /billing/meter/... 200 且 has_accounts（无 /v2 优先路径可用）
- chat：无 system 首条 → 400/11128 "first message is not system prompt"；
  加 system 后 → 403/11140 "request illegal"（displayMsg 为安全审核文案）
- OAuth platform workbuddy / CLI 双收

本探针按参考谱系保真度对照 chat 形状（最多 2 次尝试 + 末尾健康复验）：
- A 档 = panel 全量头族（会话头族 X-Conversation-* / X-Request-ID / X-Trace-ID /
  X-B3-* + X-Product: WorkBuddy + 桌面 UA 平台段 WorkBuddy AI + body stream_options）
- B 档 = OmniRoute 口径（X-Product: SaaS 最小头）——仅当 A 档失败时执行

安全：不打印 token；原始响应只落临时目录；请求数最小化。
运行：py -3.13 "docs/spec/WorkBuddy双域账号支持/探针-wb国际版chat形状.py" [导出JSON路径]
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "relay", _REPO / "relay" / "_deps"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import httpx  # noqa: E402

GLOBAL_BASE = "https://www.workbuddy.ai"
CHAT_URL = f"{GLOBAL_BASE}/v2/chat/completions"
DEFAULT_JSON = Path(r"D:\Download\codebuddy_accounts_2026-10-14.json")
OUT_DIR = Path(tempfile.gettempdir()) / "wb-intl-chat"

MODEL = "deepseek-v4.1-flash"  # 国际版目录真实模型（避开 default-model 等内部别名）


def stable_id(uid: str, purpose: str) -> str:
    return hashlib.sha256(f"wbcp:{purpose}:{uid}".encode()).hexdigest()[:36]


def load_account(path: Path) -> dict:
    doc = json.loads(path.read_text("utf-8"))
    rows = doc if isinstance(doc, list) else (doc.get("accounts") or [doc])
    if not rows:
        raise SystemExit(f"导出文件无账号: {path}")
    account = rows[0]
    print(f"[info] 账号 uid={str(account.get('uid'))[:8]}… token="
          f"{'有' if account.get('access_token') else '无'}")
    return account


def _msg_id() -> str:
    return secrets.token_hex(16)


def headers_panel(account: dict) -> dict:
    """A 档：panel 全量头族（ChatHeaders + CommonHeaders + 会话头族）。"""
    uid = str(account.get("uid") or "")
    conv_req = _msg_id()
    message_id = _msg_id()
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": GLOBAL_BASE,
        "Referer": GLOBAL_BASE + "/",
        "User-Agent": "WorkBuddy/5.5.4 WorkBuddy AI/5.5.4 CLI/2.137.1",
        "X-CodeBuddy-Request": "1",
        "Accept-Language": "en-US",
        "Authorization": f"Bearer {account.get('access_token') or ''}",
        "X-User-Id": uid,
        "X-Machine-ID": stable_id(uid, "machine"),
        "X-Session-ID": stable_id(uid, "session"),
        "X-No-Enterprise-Id": "1",
        "X-Domain": "www.workbuddy.ai",
        "X-Agent-Purpose": "conversation",
        "X-IDE-Name": "WorkBuddy",
        "X-IDE-Type": "WorkBuddy",
        "X-IDE-Version": "5.5.4",
        "X-Product": "WorkBuddy",
        "X-Conversation-Request-ID": conv_req,
        "X-Conversation-Message-ID": message_id,
        "X-Request-ID": message_id,
        "X-Root-Request-ID": conv_req,
        "X-Trace-ID": conv_req,
        "X-B3-TraceId": conv_req,
        "X-B3-SpanId": message_id[:16],
        "X-B3-Sampled": "1",
    }
    return h


def headers_omniroute(account: dict) -> dict:
    """B 档：OmniRoute 最小口径（X-Product: SaaS 必带 + 基础头）。"""
    return {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Product": "SaaS",
        "Authorization": f"Bearer {account.get('access_token') or ''}",
    }


def headers_ide_cbai(account: dict) -> dict:
    """C2 档：9router codebuddy-intl 口径（IDE 品牌 + SaaS 产品头）。"""
    uid = str(account.get("uid") or "")
    h = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": "https://www.codebuddy.ai",
        "Referer": "https://www.codebuddy.ai/",
        "User-Agent": "IDE/2.108.1 CodeBuddy/2.108.1",
        "X-Product": "SaaS",
        "X-IDE-Type": "IDE",
        "X-IDE-Name": "IDE",
        "X-CodeBuddy-Request": "1",
        "Accept-Language": "en-US",
        "Authorization": f"Bearer {account.get('access_token') or ''}",
    }
    if uid:
        h["X-User-Id"] = uid
        h["X-Machine-ID"] = stable_id(uid, "machine")
        h["X-Session-ID"] = stable_id(uid, "session")
    return h


def chat_body(system_text: str = "You are a helpful assistant.",
              typed_blocks: bool = False) -> dict:
    """chat 请求体。

    typed_blocks=True：user content 用类型化块（9router codebuddy-intl 口径：
    「CodeBuddy rejects plain OpenAI shape: needs leading system prompt + user
    content as typed blocks」）。
    """
    user_content: object = "1+1=? Answer with the number only."
    if typed_blocks:
        user_content = [{"type": "text", "text": user_content}]
    return {
        "model": MODEL,
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [
            {"role": "system", "content": system_text},
            {"role": "user", "content": user_content},
        ],
        "max_tokens": 16,
    }


async def try_chat(client: httpx.AsyncClient, tag: str, h: dict,
                   url: str = CHAT_URL, *, typed_blocks: bool = False,
                   system_text: str = "You are a helpful assistant.") -> dict:
    out: dict = {"tag": tag, "status": 0, "frames": 0, "content_chars": 0,
                 "usage": None, "error": None, "url": url}
    try:
        async with client.stream("POST", url, headers=h,
                                 json=chat_body(system_text, typed_blocks)) as resp:
            out["status"] = resp.status_code
            if resp.status_code != 200:
                text = (await resp.aread()).decode("utf-8", "replace")
                out["error"] = text[:400]
                (OUT_DIR / f"{tag}.raw.txt").write_text(text[:8000], "utf-8")
                return out
            lines: list[str] = []
            async for line in resp.aiter_lines():
                text = line.strip()
                if not text.startswith("data:"):
                    continue
                payload = text[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                out["frames"] += 1
                lines.append(payload)
                try:
                    obj = json.loads(payload)
                except ValueError:
                    continue
                if obj.get("usage"):
                    out["usage"] = obj["usage"]
                for ch in obj.get("choices") or []:
                    d = ch.get("delta") or {}
                    if isinstance(d.get("content"), str):
                        out["content_chars"] += len(d["content"])
            (OUT_DIR / f"{tag}.raw.txt").write_text("\n".join(lines)[:8000], "utf-8")
    except httpx.HTTPError as exc:
        out["status"] = -1
        out["error"] = f"{exc.__class__.__name__}: {exc}"
    return out


async def health_check(client: httpx.AsyncClient, account: dict) -> dict:
    """账号健康复验（只读目录）：确认 chat 尝试未造成账号级封禁。"""
    h = {
        "Accept": "application/json",
        "User-Agent": "WorkBuddy/5.5.4 WorkBuddy AI/5.5.4 CLI/2.137.1",
        "Authorization": f"Bearer {account.get('access_token') or ''}",
    }
    try:
        r = await client.get(f"{GLOBAL_BASE}/v3/config", headers=h)
        return {"status": r.status_code, "code": r.json().get("code")}
    except (httpx.HTTPError, ValueError) as exc:
        return {"status": -1, "error": str(exc)[:200]}


async def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_JSON
    account = load_account(path)
    results: dict = {}
    async with httpx.AsyncClient(trust_env=False, timeout=120.0) as client:
        # D 档：9router codebuddy-intl 出货口径——自家域 + IDE 头 + system 首条 +
        # 类型化 user 块 + 中性 system 文本（不用 "You are a helpful assistant."）。
        d = await try_chat(client, "D_9router_intl_typed", headers_ide_cbai(account),
                           url="https://www.codebuddy.ai/v2/chat/completions",
                           typed_blocks=True, system_text="You are CodeBuddy Code.")
        results["D_9router_intl_typed"] = d
        print(f"[D] 9router intl 类型化块（自家域）→ status={d['status']} "
              f"frames={d['frames']} err={(d['error'] or '')[:160]}")
        await asyncio.sleep(1.5)

        # D2：同口径打 workbuddy.ai（对照域——若 D 通而 D2 不通则域绑定）
        if d["status"] == 200:
            d2 = await try_chat(client, "D2_wbai_typed", headers_panel(account),
                                typed_blocks=True, system_text="You are CodeBuddy Code.")
            results["D2_wbai_typed"] = d2
            print(f"[D2] 同口径 workbuddy.ai → status={d2['status']} "
                  f"frames={d2['frames']} err={(d2['error'] or '')[:160]}")
            await asyncio.sleep(1.5)

        health = await health_check(client, account)
        results["health"] = health
        print(f"[health] 目录复验 → {health}")

    print(json.dumps(results, ensure_ascii=False, indent=2))
    print(f"[info] 原始响应落盘: {OUT_DIR}")


if __name__ == "__main__":
    asyncio.run(main())
