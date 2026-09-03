"""M5 调研：打包 exe 完整冒烟 —— 起服 → 读就绪行 → 带 Bearer 调 /v1/models。

用法: python m5_api_smoke.py <exe路径>
"""
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

exe = Path(sys.argv[1]).resolve()
data_dir = Path(tempfile.mkdtemp(prefix="m5-smoke-"))

t0 = time.perf_counter()
proc = subprocess.Popen(
    [str(exe), "run", "--port", "0"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
    env={**os.environ, "RELAY_DATA_DIR": str(data_dir)},
)
ready = None
try:
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("[relay-ready]"):
            ready = line
            break
finally:
    if not ready:
        proc.kill()
        proc.wait(timeout=10)
        print("FAIL 未读到就绪行")
        sys.exit(1)

m = re.search(r"port=(\d+) api_key=(\S+)", ready)
port, key = int(m.group(1)), m.group(2)
elapsed = time.perf_counter() - t0

# 带 Bearer 调 /v1/models（无 key 应 401）
req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/models",
                             headers={"Authorization": f"Bearer {key}"})
with urllib.request.urlopen(req, timeout=10) as resp:
    body = resp.read().decode("utf-8")
    models = len(__import__("json").loads(body).get("data", []))
print(f"OK  就绪行: {ready}")
print(f"    启动耗时: {elapsed:.2f}s")
print(f"    /v1/models 返回模型数: {models}")

# 无 key 应 401
try:
    urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=10)
    print("    FAIL 无 key 未被拒绝")
    sys.exit(1)
except urllib.error.HTTPError as e:
    print(f"    无 key → HTTP {e.code}（预期 401）")

proc.kill()
proc.wait(timeout=10)
print("冒烟通过")
