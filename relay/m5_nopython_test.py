"""M5 调研：模拟「全新机器无 Python」—— 剥离 PATH 中的 python 目录后运行打包 exe。

验证 PyInstaller 产物自包含：不依赖本机 python / PYTHONPATH / pip 包。

用法: python m5_nopython_test.py <exe路径>
"""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

exe = Path(sys.argv[1]).resolve()
assert exe.exists(), f"exe 不存在: {exe}"

# 剥离 python 相关：PATH 只留 Windows 系统目录（不含 python/venv/conda/scripts）
clean_path = r"C:\Windows\System32;C:\Windows"
env = {k: v for k, v in os.environ.items() if not k.upper().startswith("PYTHON")}
env["PATH"] = clean_path
env.pop("PYTHONHOME", None)
env.pop("PYTHONPATH", None)

data_dir = Path(tempfile.mkdtemp(prefix="m5-nopy-"))

t0 = time.perf_counter()
proc = subprocess.Popen(
    [str(exe), "run", "--port", "0"],
    stdout=subprocess.PIPE,
    stderr=subprocess.STDOUT,
    text=True,
    encoding="utf-8",
    errors="replace",
    env=env,
)
ready = None
try:
    for line in proc.stdout:
        line = line.strip()
        if line.startswith("[relay-ready]"):
            ready = line
            break
finally:
    proc.kill()
    proc.wait(timeout=10)

elapsed = time.perf_counter() - t0
if ready:
    print(f"OK  无 Python 环境下就绪: {ready}")
    print(f"    启动耗时: {elapsed:.2f}s")
else:
    print(f"FAIL 无 Python 环境下未就绪（{elapsed:.2f}s）")
    sys.exit(1)
