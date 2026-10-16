"""端口「自动顺延」冒烟（dev 与安装版并行 spec §2.2 验收脚本，本机、无上游请求）。

场景：
1) 实例 A 冷启（独立数据目录，磁盘无配置端口）→ 应落 3601，[relay-ready] port=3601；
2) 实例 B 冷启（另一独立数据目录）→ 3601 被占 → 自动顺延 3602，banner 明示顺延；
   GET /v1/auth/config 应返回 port=3601（配置起点）/ listen_port=3602（实际）+ base_url 用 3602；
3) 实例 A 重启 → 落点稳定（仍 3601）；
4) --port 9099 显式 → 落 9099 且落盘（重启不传参仍 9099）。

只用本机回环，无任何上游请求（不触牛码/WorkBuddy）。
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]  # docs/spec/<需求名>/ → 仓库根
RELAY_DIR = REPO / "relay"
PY = sys.executable


class Relay:
    """起一个 relay 子进程（独立数据目录），解析 [relay-ready] 行。"""

    def __init__(self, data_dir: Path, extra: list[str] | None = None):
        self.data_dir = data_dir
        env = dict(os.environ)
        env["RELAY_DATA_DIR"] = str(data_dir)
        env["PYTHONPATH"] = str(RELAY_DIR / "_deps")
        self.log = data_dir.parent / f"{data_dir.name}.log"
        self.log_fh = open(self.log, "w", encoding="utf-8", newline="")
        self.proc = subprocess.Popen(
            [PY, "-m", "relay", "run", *(extra or [])],
            cwd=str(RELAY_DIR), env=env,
            stdout=self.log_fh, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )

    def wait_ready(self, timeout: float = 30.0) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"relay 提前退出 rc={self.proc.returncode}\n{self.text()}")
            for line in self.text().splitlines():
                if line.startswith("[relay-ready]"):
                    info = dict(kv.split("=", 1) for kv in line.split()[1:])
                    return info
            time.sleep(0.15)
        raise RuntimeError(f"等待就绪超时\n{self.text()}")

    def text(self) -> str:
        self.log_fh.flush()
        return self.log.read_text("utf-8", errors="replace")

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.log_fh.close()


def get_config(port: int, key: str) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/auth/config",
        headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="cp-port-e2e-"))
    fails: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}{('  ' + detail) if detail else ''}")
        if not ok:
            fails.append(name)

    a = Relay(base / "dataA")
    try:
        info_a = a.wait_ready()
        check("A 冷启落 3601", info_a.get("port") == "3601", f"port={info_a.get('port')}")
        cfg_a = get_config(3601, info_a["api_key"])
        check("A config: port==listen_port==3601",
              cfg_a["port"] == 3601 and cfg_a["listen_port"] == 3601,
              f"port={cfg_a['port']} listen={cfg_a['listen_port']}")
        check("A base_url 用实际端口", ":3601/v1" in cfg_a["base_url"], cfg_a["base_url"])

        # 实例 B：另一独立数据目录，应顺延到 3602
        b = Relay(base / "dataB")
        try:
            info_b = b.wait_ready()
            check("B 顺延到 3602", info_b.get("port") == "3602", f"port={info_b.get('port')}")
            check("B banner 明示顺延", "自动顺延至 3602" in b.text())
            cfg_b = get_config(3602, info_b["api_key"])
            check("B config: port=3601 / listen_port=3602",
                  cfg_b["port"] == 3601 and cfg_b["listen_port"] == 3602,
                  f"port={cfg_b['port']} listen={cfg_b['listen_port']}")
            check("B base_url 用实际端口", ":3602/v1" in cfg_b["base_url"], cfg_b["base_url"])
            check("A 仍活着（B 未抢占）", a.proc.poll() is None)
        finally:
            b.stop()
    finally:
        a.stop()

    # A 重启：落点稳定（仍 3601）
    a2 = Relay(base / "dataA")
    try:
        info_a2 = a2.wait_ready()
        check("A 重启落点稳定", info_a2.get("port") == "3601", f"port={info_a2.get('port')}")
    finally:
        a2.stop()

    # --port 显式：9099 落盘，重启（不传参）仍 9099
    c = Relay(base / "dataC", ["--port", "9099"])
    try:
        info_c = c.wait_ready()
        check("C --port 9099 生效", info_c.get("port") == "9099", f"port={info_c.get('port')}")
    finally:
        c.stop()
    c2 = Relay(base / "dataC")
    try:
        info_c2 = c2.wait_ready()
        check("C 重启沿用落盘 9099", info_c2.get("port") == "9099", f"port={info_c2.get('port')}")
    finally:
        c2.stop()

    print("\n日志目录:", base)
    print("FAILED:", fails if fails else "无")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
