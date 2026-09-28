# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for FlowDownload.
#
# Run from the project root with the venv activated:
#   pyinstaller FlowDownload.spec
#
# Produces dist/FlowDownload.app — drag that to /Applications or to the Dock
# and double-clicking it launches the same UI as `python run.py`, no terminal
# required.

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# The spec file is run from the project root by `pyinstaller FlowDownload.spec`,
# so Path(".") resolves to the project root — keep the rest of the file in
# terms of that.
ROOT = Path(".").resolve()

# Single source of truth for the app's version. scripts/build_app.sh can
# overwrite this file (via --version) before invoking pyinstaller; running
# the build without that flag just re-embeds whatever version is already here.
APP_VERSION = (ROOT / "VERSION").read_text().strip()

# The web UI is served by the bundled ApiServer, so it has to be copied in
# alongside the Python modules. Collecting from `app/web` keeps the relative
# path the runtime expects (`app/web/index.html`). The .icns is also bundled
# into Contents/Resources so Finder "Get Info" and `defaults read` find it.
datas = [
    (str(ROOT / "app" / "web" / "index.html"), "app/web"),
    (str(ROOT / "app" / "AppIcon.icns"), "Resources"),
    # Read at runtime by app/version.py so the UI can show the build version.
    (str(ROOT / "VERSION"), "."),
]

# PySide6 ships a handful of optional plugins/QML modules; asking for the full
# submodule list avoids "Qt module not found" errors at runtime that plain
# `hiddenimports` misses.
hiddenimports = []
hiddenimports += collect_submodules("PySide6")

a = Analysis(
    ["run.py"],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    name="FlowDownload",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,         # no terminal window pops up when launched from Finder/Dock
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# Bundle the binary + dependencies into FlowDownload.app. macOS treats any
# directory ending in `.app` as a clickable application and gives it a Dock
# icon.
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FlowDownload",
)

app = BUNDLE(
    coll,
    name="FlowDownload.app",
    icon=str(ROOT / "app" / "AppIcon.icns"),  # from scripts/build_icon.py
    bundle_identifier="com.flowdownload.app",
    info_plist={
        "CFBundleName": "FlowDownload",
        "CFBundleDisplayName": "FlowDownload",
        "CFBundleShortVersionString": APP_VERSION,
        "CFBundleVersion": APP_VERSION,
        "NSHighResolutionCapable": True,
        "LSUIElement": False,  # show in Dock; set True to make it a menu-bar-only app
    },
)
