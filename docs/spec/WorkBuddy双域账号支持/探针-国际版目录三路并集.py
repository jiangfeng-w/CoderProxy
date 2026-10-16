#!/usr/bin/env python
"""WorkBuddy 国际版目录「三路 UA 并集」验证探针（2026-10-16 新增；**经伪装层出站**、小号限定）。

用途：验证 `catalog.py` 三路 UA 探测的收益与并集形态（对应 `docs/已知问题.md` BUG-012 待验证项）：
对同一国际版小号分别以 桌面档 / IDE 档 / CLI 档 UA 探测 `/v3/config`，输出三路模型集合、
并集与差集，核对参考实现结论（global：桌面 29 / IDE 13 / CLI 22，并集 25→31；
CLI 档独有 deepseek 系列/`gpt-6-astra`/`kimi-k2.8-preview`，IDE 档独有 `o4-mini`/`enhance-1.0`/`auto-chat`，
桌面档为唯一含 `gpt-6-sol`/`grok-4.7` 者）。

安全纪律（AGENTS.md 硬性规则 5/6）：
- **经伪装层出站**：复用 `relay.platforms.workbuddy.catalog._fetch_v3`（其头族/设备指纹即 relay
  出站伪装层；`ua` 参数只切换探测档位）——脚本不自己拼头、不直连上游；
- **只用小号**：`--uid` 必须显式指定（脚本不自动挑号，避免误打主账号）；只打印 uid 前 8 位；
- **请求数最小化**：恰好 3 次 GET `/v3/config`（若该号 access_token 过期会先刷新一次，属既有
  relay 逻辑）；不打印任何 token；响应不落盘。

运行（默认 dry-run，只打印计划；确需实测加 `--run`）：
    py -3.13 "docs/spec/WorkBuddy双域账号支持/探针-国际版目录三路并集.py" --uid <小号uid> --run
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
from relay.platforms.workbuddy import catalog  # noqa: E402
from relay.platforms.workbuddy.adapter import WorkBuddyAdapter  # noqa: E402
from relay.platforms.workbuddy.realms import REALM_GLOBAL  # noqa: E402


async def main(args: argparse.Namespace) -> int:
    platform = REALM_GLOBAL.platform_id
    account = await store.find_account(platform, args.uid)
    if account is None:
        print(f"[probe] 池内找不到该 uid（platform={platform}）——先在国际版 tab 用小号登录/导入")
        return 2
    print(f"[probe] 账号 uid={str(args.uid)[:8]}…  platform={platform}")
    if not args.run:
        print("[probe] dry-run：将依次以 桌面档 / IDE 档 / CLI 档 UA 各发 1 次 GET /v3/config（共 3 次）")
        print("[probe] 确认是小号、且接受 3 次只读请求后，加 --run 执行")
        return 0

    account = await WorkBuddyAdapter(REALM_GLOBAL).ensure_token(account)
    routes = (("desktop", None), ("ide", catalog._IDE_UA), ("cli", catalog._CLI_UA))
    sets: dict[str, set[str]] = {}
    for label, ua in routes:
        models = await catalog._fetch_v3(account, profile=REALM_GLOBAL, ua=ua)
        names = {str(m.get("name") or "") for m in models}
        sets[label] = names
        print(f"[probe] {label:<8} {len(names):>3} 条（过滤非对话后）")

    union = set().union(*sets.values())
    desktop_only = sets["desktop"] - sets["ide"] - sets["cli"]
    print(f"[probe] 并集 {len(union)} 条；桌面独有 {len(desktop_only)} 条：{sorted(desktop_only)}")
    print(f"[probe] IDE 补充 {len(sets['ide'] - sets['desktop'])} 条：{sorted(sets['ide'] - sets['desktop'])}")
    print(f"[probe] CLI 补充 {len(sets['cli'] - sets['desktop'])} 条：{sorted(sets['cli'] - sets['desktop'])}")
    print("[probe] 结论口径：并集 > 桌面单路 即三路合并生效（新模型随下次目录刷新进入 /v1/models）")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="国际版目录三路 UA 并集验证（经伪装层、小号限定）")
    parser.add_argument("--uid", required=True, help="国际版小号 uid（脚本不自动挑号）")
    parser.add_argument("--run", action="store_true", help="真正发请求（缺省只 dry-run 打印计划）")
    raise SystemExit(asyncio.run(main(parser.parse_args())))
