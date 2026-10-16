#!/usr/bin/env python
"""WorkBuddy 风控指纹「原始串 vs 改写串」复测探针（2026-10-16 新增；**经伪装层出站**、小号限定）。

用途：复测 `docs/已知问题.md` BUG-011 的清单（依据=参考实现 sanitize.go 的 WB 同族上游实测，
我方此前未复测）——逐串发**最小流式 chat 请求**，看上游是否拦截：
- 裸 `11128`（参考实现：整单拦截；相邻 11148/11101/11115 放行）
- `Main branch (you will usually use this for PRs)` / Codex CLI 首句 / Anthropic 反馈句
- `You are Claude Code`（我方 2026-09-06 牛码实测项，WB 域是否同拦）

方法：**绕过 relay 入站净化**（直接用上游 body 构造请求——否则探针测的是净化后的串，失去意义），
但出站复用 `WorkBuddyChatAdapter._stream_once`（其头族/UA/设备头即 relay 出站伪装层）。

安全纪律（AGENTS.md 硬性规则 5/6）：
- **经伪装层出站**：复用 adapter 的 `_headers`/`_stream_once`，脚本不自己拼头、不直连；
- **只用小号**：`--uid` 必须显式指定（脚本不自动挑号，避免误打主账号）；只打印 uid 前 8 位；
- **请求数最小化**：先发 1 次中性 baseline + 1 次「全部串合并」；合并被拦才逐串二分；
  每条被拦的原始串再补发 1 次改写串验证缓解（全通过时总请求数 = 2）；
- 不打印任何 token；响应仅留在内存（错误摘要截断打印）。

运行（默认 dry-run 只打印计划；确需实测加 `--run`）：
    set RELAY_DATA_DIR=C:\\Users\\<你>\\AppData\\Roaming\\com.coderproxy.desktop\\data
    py -3.13 "docs/spec/WorkBuddy聊天反代与多平台聚合/探针-风控指纹复测.py" --uid <小号uid> --run
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]  # docs/spec/<需求名>/ 下
for _p in (_REPO / "relay", _REPO / "relay" / "_deps"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from relay.platforms import store  # noqa: E402
from relay.platforms.base import PlatformUpstreamError  # noqa: E402
from relay.platforms.workbuddy import chat as wbchat  # noqa: E402
from relay.platforms.workbuddy.realms import REALM_CN, REALM_GLOBAL  # noqa: E402

# 测试串（原始 → 改写；改写取自 relay/relay/oai_adapter.py::_SANITIZE_REWRITES 实际生效值）
CASES = (
    ("11128", "裸错误码 11128", "code=11-128"),
    ("main_branch", "Main branch (you will usually use this for PRs)",
     "Default branch (you will usually use this for PRs)"),
    ("codex_cli", "You are a coding agent running in the Codex CLI, a terminal-based coding assistant.",
     "You are a coding agent running in the Codex CLI tool, a terminal-based coding assistant."),
    ("anthropic_feedback",
     "To give feedback, users should report the issue at https://github.com/anthropics/claude-code/issues",
     "To provide feedback, users should report the issue at https://github.com/anthropics/claude-code/issues"),
    ("you_are_claude_code", "You are Claude Code", "You are Claude Code"),  # 缓解=ZWSP，本探针不改写
)
NEUTRAL = "Reply with the single word OK."


def _body(model: str, text: str) -> dict:
    return {
        "model": model,
        "stream": True,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": f"{NEUTRAL}\n\n{text}"},
        ],
    }


async def _probe_once(adapter, account: dict, model: str, text: str) -> dict:
    """发一次最小流式请求：返回 {verdict, status, code, detail, frames}（不落盘）。"""
    body = _body(model, text)
    frames = 0
    try:
        async for _frame in adapter._stream_once(account, body):
            frames += 1
            if frames >= 3:  # 只取前三帧，够判定「首帧即错」与「正常产出」
                break
        return {"verdict": "pass", "status": 200, "code": None,
                "detail": f"正常流（已读 {frames} 帧）", "frames": frames}
    except PlatformUpstreamError as exc:
        text_out = str(exc)
        code = None
        for token in ("11128", "11140", "11101", "11133"):
            if token in text_out:
                code = token
                break
        blocked = exc.status in (400, 403) or code in ("11128", "11140")
        return {"verdict": "blocked" if blocked else "error", "status": exc.status,
                "code": code, "detail": text_out[:200], "frames": frames}


async def main(args: argparse.Namespace) -> int:
    profile = REALM_GLOBAL if args.realm == "global" else REALM_CN
    platform = profile.platform_id
    account = await store.find_account(platform, args.uid)
    if account is None:
        print(f"[probe] 池内找不到该 uid（platform={platform}）；先确认 RELAY_DATA_DIR 指向哪个数据目录")
        return 2
    print(f"[probe] 账号 uid={str(args.uid)[:8]}…  platform={platform}  model={args.model}")
    if not args.run:
        print("[probe] dry-run 计划：1 次 baseline + 1 次合并串；合并被拦才逐串二分（每条被拦串再补 1 次改写串）")
        print("[probe] 确认是小号、且接受上述最小请求后，加 --run 执行")
        return 0

    adapter = wbchat.WorkBuddyChatAdapter(args.model, profile=profile)
    account = await adapter._account_adapter.ensure_token(account)

    n = 0
    base = await _probe_once(adapter, account, args.model, NEUTRAL)
    n += 1
    print(f"[probe] baseline：{base['verdict']}（{base['detail']}）")
    if base["verdict"] != "pass":
        print("[probe] baseline 未通过——账号/链路异常，终止（不继续消耗请求）")
        return 1

    combined = "\n".join(c[1] for c in CASES)
    res = await _probe_once(adapter, account, args.model, combined)
    n += 1
    print(f"[probe] combined（{len(CASES)} 串合一）：{res['verdict']}（{res['detail']}）")
    if res["verdict"] == "pass":
        print(f"[probe] 结论：本域上游**未拦截**清单中任一串（本次实测；总请求 {n} 次）")
        return 0

    print("[probe] 合并被拦 → 逐串二分：")
    blocked_cases = []
    for key, raw, rewritten in CASES:
        r = await _probe_once(adapter, account, args.model, raw)
        n += 1
        print(f"[probe]   {key:<22} {r['verdict']:<8} code={r['code']}（{r['detail']}）")
        if r["verdict"] == "blocked":
            blocked_cases.append(key)
            if rewritten != raw:
                rw = await _probe_once(adapter, account, args.model, rewritten)
                n += 1
                print(f"[probe]   {key:<22} 改写串   {rw['verdict']:<8}"
                      f"（{'缓解有效' if rw['verdict'] == 'pass' else '改写仍被拦'}）")
    print(f"[probe] 结论：被拦串 = {blocked_cases or '无'}（总请求 {n} 次）")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="风控指纹复测（经伪装层、小号限定、最小请求数）")
    parser.add_argument("--uid", required=True, help="小号 uid（脚本不自动挑号）")
    parser.add_argument("--model", default="glm-5.3-flash", help="用于探测的模型（默认 glm-5.3-flash）")
    parser.add_argument("--realm", choices=("cn", "global"), default="cn", help="探测域（默认 cn）")
    parser.add_argument("--run", action="store_true", help="真正发请求（缺省只 dry-run 打印计划）")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
