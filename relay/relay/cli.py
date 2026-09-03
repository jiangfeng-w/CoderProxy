"""CLI 入口：`python -m relay run [--port 8786] [--api-base ...]`。

子命令：
  run          启动 FastAPI relay 服务（默认 127.0.0.1:8786）
  login        触发登录（IM 静默优先，降级浏览器 PKCE 并自动打开），轮询至完成
  logout       清除本地登录态
  sync         同步模型目录（需已登录）
  config       打印中转配置（base_url / api_key）

环境变量见 relay/config.py；依赖自举见 docs/README.md「自举环境」。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _bootstrap() -> None:
    """把项目根与本地依赖目录加入 sys.path（无论 cwd 在哪都能 import）。"""
    for p in (_PROJECT_ROOT, _PROJECT_ROOT / "_deps"):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


def _setup_logging(level: int = logging.INFO) -> None:
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def _cmd_run(args) -> int:
    from relay.config import settings
    from relay.storage import ensure_initialized

    if args.port is not None:
        settings.relay_port = args.port
    if args.api_base is not None:
        settings.ta3_api_base = args.api_base
    if args.host is not None:
        settings.relay_host = args.host

    # 首次启动：生成并持久化 RELAY_API_KEY（agent 里填这个）
    asyncio.run(ensure_initialized())

    import uvicorn

    print(f"* Relay 已就绪: http://{settings.relay_host}:{settings.relay_port}/v1")
    print(f"* API Key   : {settings.relay_api_key or '(环境变量 RELAY_API_KEY)'}")
    print("* 模型列表  : GET  /v1/models")
    print("* 聊天      : POST /v1/chat/completions")
    print("* 登录       : POST /v1/auth/login/start（或 CLI: python -m relay login）")
    uvicorn.run("relay.routes:app", host=settings.relay_host,
                port=settings.relay_port, log_level="info")


def _cmd_login(args) -> int:
    from relay import auth_flow

    async def _run():
        import webbrowser

        result = await auth_flow.start_login()
        if result.get("status") == "logged_in":
            print(f"✅ 已登录（source={result.get('source')}）: {result.get('account')}")
            return
        url = result.get("authorize_url", "")
        print("请在弹出的浏览器中完成登录；若未自动打开，请手动访问：")
        print(url)
        webbrowser.open(url)
        print("等待登录完成（Ctrl+C 取消）...")
        try:
            while True:
                await asyncio.sleep(2)
                st = await auth_flow.login_status()
                if st.get("status") == "logged_in":
                    print(f"✅ 登录成功: {st.get('account')}")
                    return
                if st.get("status") == "failed":
                    print(f"❌ 登录失败: {st.get('error')}")
                    return
        except KeyboardInterrupt:
            await auth_flow.cancel_login()
            print("\n已取消")

    return asyncio.run(_run())


def _cmd_logout(args) -> int:
    from relay import auth_flow

    asyncio.run(auth_flow.logout())
    print("已退出登录（本地登录态已清除）")
    return 0


def _cmd_sync(args) -> int:
    from relay import auth_flow

    try:
        models = asyncio.run(auth_flow.sync_models())
    except Exception as e:  # noqa: BLE001
        print(f"❌ 目录同步失败: {e}")
        return 1
    print(f"✅ 同步出 {len(models)} 个模型:")
    for m in models:
        tag = "anthropic" if m.get("anthropic") else "openai"
        print(f"  - {m['name']}  ({tag}, ctx={m.get('context_window')})")
    return 0


def _cmd_config(args) -> int:
    from relay.config import settings
    from relay.storage import relay_api_key

    print(f"base_url = http://{settings.relay_host}:{settings.relay_port}/v1")
    print(f"api_key  = {relay_api_key()}")
    print(f"tool_mode= {settings.tool_mode}")
    return 0


def main(argv=None) -> int:
    _bootstrap()
    parser = argparse.ArgumentParser(prog="python -m relay", description="CoderProxy relay CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="启动 relay 服务")
    p_run.add_argument("--host", help="监听地址（默认 RELAY_HOST / 127.0.0.1）")
    p_run.add_argument("--port", type=int, help="监听端口（默认 RELAY_PORT / 8786）")
    p_run.add_argument("--api-base", help="牛码 API 基址（默认 TA3_API_BASE）")

    sub.add_parser("login", help="触发登录并等待完成")
    sub.add_parser("logout", help="退出登录")
    sub.add_parser("sync", help="同步模型目录")
    sub.add_parser("config", help="打印中转配置")

    args = parser.parse_args(argv)
    _setup_logging(logging.DEBUG if getattr(args, "verbose", False) else logging.INFO)
    cmd = args.command
    if cmd == "run":
        return _cmd_run(args)
    if cmd == "login":
        return _cmd_login(args)
    if cmd == "logout":
        return _cmd_logout(args)
    if cmd == "sync":
        return _cmd_sync(args)
    if cmd == "config":
        return _cmd_config(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
