"""端口「配置起点 + 自动顺延」单测（dev 与安装版并行 spec §2.2）。

覆盖：
- 起点优先级四元：`--port` 显式（落盘持久化）> 磁盘配置端口 > env/默认（不落盘）；
- `storage.get_saved_port` 边界（未保存/非法 → None；get_port 仍回退默认）；
- 顺延：起点被占 → 下一空位；窗口内全占 → SystemExit；connect 活性判定口径。

全部 keyless、不触发上游网络（仅本机回环 socket）。原「固定端口预检即退」
用例（`cli._check_port_available`）随端口语义变更迁到本文件。
"""
from __future__ import annotations

import asyncio
import socket

import pytest
import pytest_asyncio

from relay import cli, storage


@pytest_asyncio.fixture
async def port_storage(tmp_path, monkeypatch):
    """隔离磁盘状态：storage 读写指向 tmp_path，并复位内存里的配置起点。"""
    monkeypatch.setattr(storage.settings, "data_dir", str(tmp_path))
    monkeypatch.setattr(storage.settings, "relay_port_configured", None)
    return storage


def _listener() -> tuple[socket.socket, int]:
    """起一个真空闲监听（占用一个随机端口），返回 (socket, port)。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


# ─────────────────────────── 起点解析：四元优先级 ───────────────────────────

async def test_resolve_port_explicit_wins_and_persists(port_storage):
    await port_storage.save_port(9090)  # 先有磁盘配置
    start = await cli._resolve_port_start(9100, 9111)  # --port > 磁盘 > env
    assert start == 9100
    assert await port_storage.get_saved_port() == 9100  # 显式传参落盘（现有语义）
    assert port_storage.settings.relay_port_configured == 9100


async def test_resolve_port_disk_over_env(port_storage):
    await port_storage.save_port(9090)
    assert await cli._resolve_port_start(None, 9111) == 9090


async def test_resolve_port_env_when_disk_empty_not_persisted(port_storage):
    assert await port_storage.get_saved_port() is None
    assert await cli._resolve_port_start(None, 9111) == 9111
    # env 派生值只作本次起点，不落盘（只有配置端口落盘）
    assert await port_storage.get_saved_port() is None


async def test_resolve_port_default_fallback(port_storage):
    assert await cli._resolve_port_start(None, storage.DEFAULT_PORT) == storage.DEFAULT_PORT


async def test_resolve_port_explicit_invalid_raises(port_storage):
    with pytest.raises(SystemExit, match="非法"):
        await cli._resolve_port_start(0, storage.DEFAULT_PORT)
    assert await port_storage.get_saved_port() is None  # 非法值不落盘


# ─────────────────────────── storage.get_saved_port 边界 ───────────────────────────

async def test_get_saved_port_boundaries(port_storage):
    assert await port_storage.get_saved_port() is None
    await port_storage.save_port(9090)
    assert await port_storage.get_saved_port() == 9090
    await port_storage.save_port(0)  # 非法（防御损坏状态）
    assert await port_storage.get_saved_port() is None
    assert await port_storage.get_port() == storage.DEFAULT_PORT  # 兼容口径不变
    await port_storage.save_port(70000)
    assert await port_storage.get_saved_port() is None


def test_cli_config_prints_saved_port(port_storage, monkeypatch, capsys):
    """`python -m relay config` 与起服同口径读磁盘（原先只读 env/默认，
    配置页存过端口后本命令仍报 3601，agent 会抄到错地址）。"""
    monkeypatch.setattr(storage.settings, "relay_host", "127.0.0.1")
    asyncio.run(port_storage.save_port(13601))
    asyncio.run(port_storage.save_tool_mode("strict"))
    cli._cmd_config(None)
    out = capsys.readouterr().out
    assert "http://127.0.0.1:13601/v1" in out
    assert "tool_mode= strict" in out


# ─────────────────────────── 顺延扫描与独占绑定 ───────────────────────────

def test_probe_host_maps_wildcard():
    assert cli._probe_host("0.0.0.0") == "127.0.0.1"
    assert cli._probe_host("") == "127.0.0.1"
    assert cli._probe_host("*") == "127.0.0.1"
    assert cli._probe_host("::") == "::1"
    assert cli._probe_host("127.0.0.1") == "127.0.0.1"
    assert cli._probe_host("::1") == "::1"


def _ipv6_available() -> bool:
    try:
        socket.socket(socket.AF_INET6, socket.SOCK_STREAM).close()
        return True
    except OSError:
        return False


def _free_port(family: int, bind_host: str) -> int:
    s = socket.socket(family, socket.SOCK_STREAM)
    s.bind((bind_host, 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_pick_listen_socket_ipv4_wildcard_host():
    port = _free_port(socket.AF_INET, "127.0.0.1")
    sock, picked = cli.pick_listen_socket("0.0.0.0", port)
    try:
        assert picked == port
        assert sock.family == socket.AF_INET
    finally:
        sock.close()


@pytest.mark.skipif(not _ipv6_available(), reason="本机无 IPv6")
def test_pick_listen_socket_ipv6_hosts():
    """IPv6 字面量主机名（:: / ::1）必须可绑——历史坑：写死 AF_INET 会误报「均被占用」。"""
    port = _free_port(socket.AF_INET6, "::1")
    sock, picked = cli.pick_listen_socket("::1", port)
    try:
        assert picked == port
        assert sock.family == socket.AF_INET6
    finally:
        sock.close()
    sock, picked = cli.pick_listen_socket("::", port)
    try:
        assert picked == port  # 端口空间与 IPv4 独立，可复用同一号
        assert sock.family == socket.AF_INET6
    finally:
        sock.close()


def test_bind_backstops_probe_blind_spot():
    """探测盲区兜底：活监听的 accept 队列被打满时 connect 会超时（判「不占用」，
    本机实测），但独占绑定必然失败 → 扫描仍会跳过该口（不劫持、不误用）。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        cli._probe_occupied("127.0.0.1", port)  # 无断言：是否超时取决于队列状态
        cli._probe_occupied("127.0.0.1", port)
        assert cli._bind_exclusive("127.0.0.1", port) is None
        sock, picked = cli.pick_listen_socket("127.0.0.1", port)
        try:
            assert picked != port
        finally:
            sock.close()
    finally:
        s.close()


def test_probe_occupied_true_for_listener_false_when_quiet():
    s, port = _listener()
    try:
        assert cli._probe_occupied("127.0.0.1", port) is True
    finally:
        s.close()
    # 关闭后无活监听：本机可能「被拒」也可能「静默超时」（实测两者都有），
    # 两种都不是「连通」→ 判未占用
    assert cli._probe_occupied("127.0.0.1", port) is False


def test_pick_listen_socket_skips_busy_start():
    s, busy = _listener()
    try:
        sock, picked = cli.pick_listen_socket("127.0.0.1", busy)
    finally:
        s.close()
    try:
        assert busy < picked < busy + cli.PORT_SCAN_WINDOW
        assert sock.getsockname()[1] == picked  # 返回的正是已绑定的口
    finally:
        sock.close()


def test_pick_listen_socket_accepts_free_start():
    # 取一个确定空闲的口（bind→close 后未监听），应原样返回（不顺延）
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    sock, picked = cli.pick_listen_socket("127.0.0.1", free)
    try:
        assert picked == free
    finally:
        sock.close()


def test_pick_listen_socket_window_one_busy_raises():
    s, busy = _listener()
    try:
        with pytest.raises(SystemExit, match="均被占用"):
            cli.pick_listen_socket("127.0.0.1", busy, window=1)
    finally:
        s.close()


def test_pick_listen_socket_all_busy_raises(monkeypatch):
    monkeypatch.setattr(cli, "_probe_occupied", lambda *a, **k: True)
    with pytest.raises(SystemExit, match="均被占用"):
        cli.pick_listen_socket("127.0.0.1", 3601)


def test_pick_listen_socket_bind_backstops_probe_false_negative(monkeypatch):
    """探测漏报（如被防火墙挡住看不到的活监听）时，独占绑定兜底：跳过该口。"""
    s, busy = _listener()
    monkeypatch.setattr(cli, "_probe_occupied", lambda *a, **k: False)  # 假装都空闲
    try:
        sock, picked = cli.pick_listen_socket("127.0.0.1", busy)
    finally:
        s.close()
    try:
        assert picked != busy  # 绑定 busy 必失败（对方活监听）→ 顺延
        assert busy < picked < busy + cli.PORT_SCAN_WINDOW
    finally:
        sock.close()


def test_pick_listen_socket_zero_or_invalid_raises():
    with pytest.raises(SystemExit, match="0"):
        cli.pick_listen_socket("127.0.0.1", 0)
    with pytest.raises(SystemExit, match="非法"):
        cli.pick_listen_socket("127.0.0.1", 70000)
