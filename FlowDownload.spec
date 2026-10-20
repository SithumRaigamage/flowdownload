# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for FlowDownload.
#
# Run from the project root with the venv activated:
#   pyinstaller FlowDownload.spec
#
# Produces dist/FlowDownload.app on macOS — drag that to /Applications or to
# the Dock and double-clicking it launches the same UI as `python run.py`, no
# terminal required. On Windows and Linux it produces the folder
# dist/FlowDownload/ (FlowDownload[.exe] + flowdownload-host[.exe]), which
# packaging/ turns into an installer / AppImage.

import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

# The spec file is run from the project root by `pyinstaller FlowDownload.spec`,
# so Path(".") resolves to the project root — keep the rest of the file in
# terms of that.
ROOT = Path(".").resolve()
IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform == "win32"

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
    (str(ROOT / "app" / "web" / "fonts"), "app/web/fonts"),
    # Read at runtime by app/version.py so the UI can show the build version.
    (str(ROOT / "VERSION"), "."),
    # The Chrome extension, so "Load unpacked" can point inside the app
    # instead of at a source checkout (see scripts/setup_chrome_integration.py).
    (str(ROOT / "chrome_extension"), "chrome_extension"),
]
if IS_MAC:
    datas.append((str(ROOT / "app" / "AppIcon.icns"), "Resources"))

# The executable's own icon (macOS takes it from BUNDLE below instead).
EXE_ICON = str(ROOT / "app" / ("AppIcon.ico" if IS_WINDOWS else "AppIcon.png"))

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
    console=False,         # no terminal window pops up when launched from Finder/Dock/Start menu
    disable_windowed_traceback=False,
    icon=None if IS_MAC else EXE_ICON,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# The Chrome native messaging host, as a second executable in the same
# bundle (Contents/MacOS/flowdownload-host). Without it, the Chrome
# integration ran native_host/flowdownload_host.py out of the source
# checkout with its venv's Python, so it broke as soon as the repo or venv
# moved. It never imports Qt (see app/engine/queueing.py), so PySide6 is
# excluded to keep it starting fast on every hand-off.
host_a = Analysis(
    ["native_host/flowdownload_host.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["PySide6", "shiboken6", "fastapi", "uvicorn", "starlette", "pydantic"],
    noarchive=False,
)
host_pyz = PYZ(host_a.pure, host_a.zipped_data, cipher=block_cipher)
host_exe = EXE(
    host_pyz,
    host_a.scripts,
    [],
    exclude_binaries=True,
    name="flowdownload-host",
    debug=False,
    strip=False,
    upx=False,
    console=True,  # Chrome talks to it over stdin/stdout
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

# Collect the binaries + dependencies into dist/FlowDownload/; on macOS that
# is then wrapped into FlowDownload.app. macOS treats any directory ending in
# `.app` as a clickable application and gives it a Dock icon.
coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    host_exe,
    host_a.binaries,
    host_a.zipfiles,
    host_a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="FlowDownload",
)

if IS_MAC:
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
