"""M5 调研：独立运行打包后的 relay-sidecar.exe，验证 [relay-ready] 协议与启动耗时。

用法: python m5_pack_test.py <exe路径>
"""
import subprocess
import sys
import tempfile
import time
from pathlib import Path

exe = Path(sys.argv[1]).resolve()
assert exe.exists(), f"exe 不存在: {exe}"

# 数据目录放临时目录，模拟「全新机器、无 relay/data」
data_dir = Path(tempfile.mkdtemp(prefix="m5-relay-"))

t0 = time.perf_counter()
proc = subprocess.Popen(
    [str(exe), "run", "--port", "0"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
    env={**__import__("os").environ, "RELAY_DATA_DIR": str(data_dir)},
)
ready = None
try:
    for line in proc.stdout:  # 逐行读，直到 [relay-ready]
        line = line.strip()
        print(f"[stdout] {line}")
        if line.startswith("[relay-ready]"):
            ready = line
            break
finally:
    proc.kill()
    proc.wait(timeout=10)

elapsed = time.perf_counter() - t0
if ready:
    print(f"\nOK  就绪行: {ready}")
    print(f"    启动耗时: {elapsed:.2f}s")
else:
    print(f"\nFAIL 未读到就绪行 (启动耗时 {elapsed:.2f}s)")
    sys.exit(1)
