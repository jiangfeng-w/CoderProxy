# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：relay sidecar 单文件模式(onefile) —— M5 对比用。

构建产物：dist/relay-sidecar-onefile/relay-sidecar-onefile.exe
与 relay.spec（onedir）对比体积 / 启动耗时，定 Tauri externalBin 集成方案。
"""
import sys
from pathlib import Path

block_cipher = None

RELAY_DIR = Path(SPECPATH).resolve()          # relay/
DEPS_DIR = RELAY_DIR / "_deps"

hiddenimports = [
    "sqlite3",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "h11",
    "pydantic",
    "relay.routes",
]

a = Analysis(
    [str(RELAY_DIR / "run.py")],
    pathex=[str(RELAY_DIR), str(DEPS_DIR)],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest", "_pytest", "pytest_asyncio",
        "tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
        "IPython", "notebook", "ruff", "mypy",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="relay-sidecar-onefile",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
