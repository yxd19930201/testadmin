# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：把 captrace 后端 + pywebview 桌面壳 + assets 资源打包为单目录 exe。"""
import os
from pathlib import Path

ROOT = Path(SPECPATH).resolve()  # 项目根 = testadmin
ASSETS = ROOT / "assets"

a = Analysis(
    [str(ROOT / "captrace" / "app_gui.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ASSETS / "app.html"), "assets"),
        (str(ASSETS / "icon.ico"), "assets"),
    ],
    hiddenimports=[
        "captrace.main",
        "captrace.app_gui",
        "captrace.checker.matrix",
        "captrace.checker.api_check",
        "captrace.checker.rules",
        "captrace.config.models",
        "captrace.config.settings",
        "captrace.storage.projects",
        "captrace.ai.analyzer",
        "captrace.connectors.ai",
        "captrace.capture.receiver",
        "captrace.capture.saz_import",
        "webview",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="captrace-gui",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,                     # 桌面应用，不弹黑框
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ASSETS / "icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="captrace-gui",
    contents_directory=None,          # 平铺到 dist/captrace-gui 根，便于运行时用 BASE_DIR/assets 定位
)
