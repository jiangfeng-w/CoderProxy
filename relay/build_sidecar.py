"""M5：打包 relay sidecar（PyInstaller onefile）并拷贝到 src-tauri/binaries/。

用法: python build_sidecar.py [--keep-ondir]
- onefile 产物: relay/dist/relay-sidecar-onefile.exe → src-tauri/binaries/relay-sidecar-<triple>.exe
- --keep-ondir: 额外构建 onedir（relay.spec，调研/排障用），不影响 externalBin 集成

target triple 与壳一致：x86_64-pc-windows-msvc（Tauri externalBin 依赖该后缀）。
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent          # relay/
TAURI_BIN = ROOT.parent / "src-tauri" / "binaries"
TRIPLE = "x86_64-pc-windows-msvc"
ONEFILE_NAME = "relay-sidecar-onefile"


def _build(spec: str) -> None:
    subprocess.run([sys.executable, "-m", "PyInstaller", "--clean", "--noconfirm", spec],
                   cwd=ROOT, check=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep-ondir", action="store_true", help="额外构建 onedir（排障用）")
    args = ap.parse_args()

    print("[build_sidecar] 构建 onefile ...")
    _build("relay-onefile.spec")

    src = ROOT / "dist" / f"{ONEFILE_NAME}.exe"
    dst = TAURI_BIN / f"relay-sidecar-{TRIPLE}.exe"
    TAURI_BIN.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    print(f"[build_sidecar] 拷贝到 {dst}（{dst.stat().st_size / 1e6:.1f} MB）")

    if args.keep_ondir:
        print("[build_sidecar] 构建 onedir ...")
        _build("relay.spec")
        print(f"[build_sidecar] onedir: {ROOT / 'dist' / 'relay-sidecar'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
