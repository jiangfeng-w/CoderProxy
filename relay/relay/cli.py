"""CLI 入口：`python -m relay run [--port 3601] [--api-base ...] [--api-key ...]`。

子命令：
  run          启动 FastAPI relay 服务（默认从 localhost:3601 起，被占用自动顺延）
               --port 指定「配置端口」起点并持久化（不传则用上次保存的端口）；
               --api-key 设置并持久化 key
  login        触发登录（IM 静默优先，降级浏览器 PKCE 并自动打开），轮询至完成
  logout       清除本地登录态
  sync         同步模型目录（需已登录）
  config       打印中转配置（base_url / api_key）

端口口径（dev 与安装版并行 spec §2.2「各自配置 + 自动顺延」）：起点 =
`--port` 显式 > 磁盘「配置端口」> `RELAY_PORT` env > 默认 3601；监听地址默认
`localhost`（IPv4+IPv6 回环都绑，agent 无论先解析到谁都能连）；从起点向上
connect 快速跳过活监听、独占绑定找第一个空位（窗口 PORT_SCAN_WINDOW），命中
后把已绑定 socket 交 uvicorn（探测→绑定无空窗）。只有配置端口落盘，实际端口
不落盘。

环境变量见 relay/config.py；依赖自举见 docs/README.md「自举环境」。
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 顺延窗口：从配置起点向上最多试这么多个端口，全占则启动失败并列明范围
PORT_SCAN_WINDOW = 20
# connect 活性探测超时（本机回环，正常毫秒级；超时按「占用」保守处理）
_PORT_PROBE_TIMEOUT_S = 0.3


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


def _addr_infos(host: str, port: int, *, passive: bool) -> list[tuple]:
    """解析监听/探测地址：**由 getaddrinfo 决定地址族**（IPv4 / IPv6 / 主机名都支持）。

    返回**全部**去重地址（不取单个）：`localhost` 在本机解析为 `::1` + `127.0.0.1`
    两条，两个回环都绑才能保证 agent 无论先解析到谁都能连上（本机实测 ::1 优先）。
    历史坑（2026-10-16 自查修复）：原先写死 AF_INET，`RELAY_HOST=::` / `::1` 时
    探测与绑定全失败 → 误报「端口均被占用」且起不来（旧路径由 uvicorn 绑，IPv6 正常）。
    """
    try:
        infos = socket.getaddrinfo(host or None, port, type=socket.SOCK_STREAM,
                                   flags=socket.AI_PASSIVE if passive else 0)
    except OSError:
        return []
    out, seen = [], set()
    for family, socktype, proto, _, sockaddr in infos:
        key = (family, sockaddr[0])
        if key in seen:
            continue
        seen.add(key)
        out.append((family, socktype, proto, sockaddr))
    return out


# 通配监听地址 → 探测用回环（1:1 映射，保持地址族）
_WILDCARD_TO_LOOPBACK = {"0.0.0.0": "127.0.0.1", "::": "::1"}


def _probe_targets(host: str, port: int) -> list[tuple]:
    """探测目标地址列表：通配地址按回环探测；主机名保留其全部解析结果。"""
    return [
        (family, socktype, proto,
         (_WILDCARD_TO_LOOPBACK.get(sockaddr[0], sockaddr[0]), sockaddr[1]))
        for family, socktype, proto, sockaddr in _addr_infos(host, port, passive=False)
    ]


def _probe_occupied(host: str, port: int, timeout: float = _PORT_PROBE_TIMEOUT_S) -> bool:
    """connect 快速判定：**任一地址连通 = 确定被占用**（活监听必然握手成功）。

    未连通不反推「空闲」：本机实测（2026-10-16）连接未监听端口既可能是被拒，
    也可能被本机安全软件静默丢 SYN 而超时——超时与「空闲」不可区分，故只把
    「连通」当正向证据用于快速跳过，真正的空闲由 _bind_exclusive 落实。
    地址解不出（非法主机名等）时按「不占用」处理，交给独占绑定兜底报错。
    """
    for family, socktype, proto, sockaddr in _probe_targets(host, port):
        with socket.socket(family, socktype, proto) as s:
            s.settimeout(timeout)
            try:
                s.connect(sockaddr)
            except OSError:
                continue  # 该地址无监听（或被拒/超时）→ 看下一个
            return True
    return False


def _bind_exclusive(host: str, port: int) -> list[socket.socket] | None:
    """独占绑定（**不加** SO_REUSEADDR）：成功 = 该端口确属空闲且由本进程持有。

    为什么每个地址都不加 SO_REUSEADDR（spec §2.2.1.4 的实测修订，2026-10-16 本机矩阵）：
    ① 对「同样带 REUSEADDR 的活监听」（uvicorn 默认带）REUSEADDR 绑定**会成功**
       = 劫持对方端口；不加则任何活监听都以 WSAEADDRINUSE/WSAEACCES 失败。
    ② TIME_WAIT 残留不影响独占绑定（服务端 TIME_WAIT 场景实测绑定仍 ok），
       故无需为「快速重启」牺牲排他性。

    多地址（`localhost` = IPv4+IPv6 回环）**全绑或全不绑**：任一条失败即释放全部
    并返回 None（视作该端口被占用，交给扫描顺延）。解不出地址同样返回 None。
    """
    infos = _addr_infos(host, port, passive=True)
    if not infos:
        return None
    socks: list[socket.socket] = []
    for family, socktype, proto, sockaddr in infos:
        s = socket.socket(family, socktype, proto)
        try:
            s.bind(sockaddr)
        except OSError:
            s.close()
            for t in socks:
                t.close()
            return None
        socks.append(s)
    return socks


def pick_listen_socket(host: str, start: int,
                       window: int = PORT_SCAN_WINDOW) -> tuple[list[socket.socket], int]:
    """从起点向上扫出第一个可独占绑定的端口，返回 (已绑定 socket 列表, 端口)。

    起点被占（如安装版已占 3601）→ 顺延到下一个空位；窗口内全占 → SystemExit
    并列明范围。探测与绑定在同一函数内完成（先 connect 快速跳过活监听，再独占
    绑定落实归属），调用方拿到 socket 直接交 uvicorn——「探测→绑定」的 TOCTOU
    空窗不存在。`localhost` 这类多地址主机返回多个 socket（一次全绑）。
    """
    if start == 0:
        raise SystemExit("端口不能为 0（请在配置页设置固定端口）")
    if not 1 <= start <= 65535:
        raise SystemExit(f"端口 {start} 非法（需 1-65535，请在配置页修改）")
    end = min(start + window, 65536)
    for port in range(start, end):
        if _probe_occupied(host, port):
            continue  # 活监听：快速跳过（不必等 bind 报错）
        socks = _bind_exclusive(host, port)
        if socks is not None:
            return socks, port
    raise SystemExit(
        f"端口 {start}–{end - 1} 均被占用，无法自动顺延；请在配置页换一段端口后重启"
    )


async def _resolve_port_start(explicit: int | None, env_or_default: int) -> int:
    """端口起点：`--port` 显式（落盘持久化）> 磁盘 > env/默认（不落盘）。"""
    from relay.storage import get_saved_port, save_port

    if explicit is not None:
        if not 1 <= explicit <= 65535:
            raise SystemExit(f"端口 {explicit} 非法（需 1-65535）")
        await save_port(explicit)
        return explicit
    saved = await get_saved_port()
    return saved if saved is not None else env_or_default


def _cmd_run(args) -> int:
    from relay.config import settings, url_host
    from relay.storage import (apply_persisted_config, ensure_initialized,
                               save_api_key, save_tool_mode)

    if args.api_base is not None:
        settings.ta3_api_base = args.api_base
    if args.host is not None:
        settings.relay_host = args.host
    if args.api_key is not None:
        settings.relay_api_key = args.api_key

    # 首次启动：生成并持久化 RELAY_API_KEY（agent 里填这个）
    asyncio.run(ensure_initialized())

    # D1 启动装载：磁盘持久化配置优先（tool_mode 等），env/默认仅兜底。
    # GUI（壳 spawn sidecar = 本命令无参）由此读到上次保存的模式。
    asyncio.run(apply_persisted_config())
    if args.tool_mode is not None:
        # CLI 显式指定 = 落盘持久化（对齐 --port 语义），并覆盖磁盘装载结果
        settings.tool_mode = args.tool_mode
        asyncio.run(save_tool_mode(args.tool_mode))

    # CLI 显式 --api-key：覆盖并持久化（GUI 配置页「生成/复制」等价物）
    if args.api_key is not None:
        asyncio.run(save_api_key(args.api_key))

    # 端口：起点（--port > 磁盘 > env/默认）→ 向上扫空位 → 独占绑定交 uvicorn。
    # settings.relay_port 恒 = 实际监听（[relay-ready] / 壳转发 / 顶栏自动正确）
    settings.relay_port_configured = asyncio.run(
        _resolve_port_start(args.port, settings.relay_port))
    listen_socks, actual_port = pick_listen_socket(
        settings.relay_host, settings.relay_port_configured)
    if actual_port != settings.relay_port_configured:
        print(f"* 端口 {settings.relay_port_configured} 已被占用，自动顺延至 {actual_port}",
              flush=True)
    settings.relay_port = actual_port

    import uvicorn

    # flush=True：被 GUI sidecar spawn 时 stdout 是管道，块缓冲会吞掉 banner
    print(f"* Relay 已就绪: http://{url_host(settings.relay_host)}:{settings.relay_port}/v1",
          flush=True)
    print(f"* API Key   : {settings.relay_api_key or '(环境变量 RELAY_API_KEY)'}", flush=True)
    print("* 模型列表  : GET  /v1/models", flush=True)
    print("* 聊天      : POST /v1/chat/completions", flush=True)
    print("* 登录       : POST /v1/auth/login/start（或 CLI: python -m relay login）", flush=True)
    # 预绑定 socket 交 uvicorn（内部自行 listen；localhost = 回环双栈多枚 socket）
    server = uvicorn.Server(uvicorn.Config("relay.routes:app", log_level="info"))
    try:
        server.run(sockets=listen_socks)
    finally:
        for s in listen_socks:
            s.close()
    return 0


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
    from relay.config import settings, url_host
    from relay.storage import apply_persisted_config, get_saved_port, relay_api_key

    # 与起服同口径读磁盘（原先只读 env/默认 → 配置页存过端口/模式后本命令仍报旧值）
    asyncio.run(apply_persisted_config())
    saved_port = asyncio.run(get_saved_port())
    port = saved_port if saved_port is not None else settings.relay_port
    print(f"base_url = http://{url_host(settings.relay_host)}:{port}/v1")
    print(f"api_key  = {relay_api_key()}")
    print(f"tool_mode= {settings.tool_mode}")
    print("（以上为配置端口；实际监听以运行中实例的 /v1/auth/config → listen_port 为准）")
    return 0


def main(argv=None) -> int:
    _bootstrap()
    parser = argparse.ArgumentParser(prog="python -m relay", description="CoderProxy relay CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="启动 relay 服务")
    p_run.add_argument("--host", help="监听地址（默认 RELAY_HOST / localhost，回环双栈）")
    p_run.add_argument("--port", type=int, help="监听端口（默认 RELAY_PORT / 3601）")
    p_run.add_argument("--api-base", help="牛码 API 基址（默认 TA3_API_BASE）")
    p_run.add_argument("--tool-mode", choices=["hybrid", "strict", "passthrough"],
                       help="工具伪装模式（默认 TOOL_MODE / hybrid）")
    p_run.add_argument("--api-key", help="设置并持久化 RELAY API Key（GUI 配置页等价物）")

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
