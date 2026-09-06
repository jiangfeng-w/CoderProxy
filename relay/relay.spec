# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：relay sidecar 单目录模式(onedir)。

构建产物：dist/relay-sidecar/relay-sidecar.exe
Tauri 壳 spawn 该 exe，监听 127.0.0.1:<port>，stdout 输出 [relay-ready] 行。

依赖说明：
- relay 第三方依赖全部 vendored 在 relay/_deps/（fastapi/uvicorn/httpx/pydantic 等），
  无需 pip 全局安装 → pathex 同时指向 relay 根 与 _deps。
- uvicorn 各子模块（logging/loops/protocols/websockets）为运行时 import，
  PyInstaller 静态分析扫不到，需显式 hiddenimports（实测踩过的坑）。
- app/auth/ta3/catalog.py 内 sync_ta3_models 惰性 import sqlalchemy，
  但 relay 走 catalog_sync.py（无 DB），该函数永不执行，缺失 sqlalchemy 无碍。
- 数据目录禁止落在解包目录：调用方须传 RELAY_DATA_DIR（run.py frozen 分支注释）。
"""
import os
import sys
from pathlib import Path

block_cipher = None

RELAY_DIR = Path(SPECPATH).resolve()          # relay/
DEPS_DIR = RELAY_DIR / "_deps"

hiddenimports = [
    "sqlite3",
    # uvicorn 子模块运行时 import，静态分析扫不到（实测踩过的坑）
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
    # cli.py 用 uvicorn.run("relay.routes:app") 字符串导入，静态分析不追踪 →
    # 显式列出，连带分析 routes.py 的传递依赖（oai_adapter/auth_flow/tool_disguise/monitor...）
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
    [],
    exclude_binaries=True,
    name="relay-sidecar",
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

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="relay-sidecar",
)
