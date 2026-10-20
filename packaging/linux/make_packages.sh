#!/usr/bin/env bash
# make_packages.sh — turn dist/FlowDownload/ into an AppImage and a tarball.
#
# Usage (from the project root, after `pyinstaller FlowDownload.spec`):
#   packaging/linux/make_packages.sh <version>
# Output: dist/FlowDownload-<version>-linux-x86_64.AppImage
#         dist/FlowDownload-<version>-linux-x86_64.tar.gz
#
# Needs appimagetool on PATH (the release workflow downloads it).

set -euo pipefail

version="${1:?usage: make_packages.sh <version>}"
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
build="dist/FlowDownload"
[[ -x "$build/FlowDownload" ]] || { echo "error: $build not found -- run pyinstaller first" >&2; exit 1; }
name="FlowDownload-${version}-linux-x86_64"

# Plain tarball: unpack anywhere and run ./FlowDownload/FlowDownload.
tar -C dist -czf "dist/${name}.tar.gz" FlowDownload

# AppImage: the PyInstaller folder under usr/lib, plus the AppDir metadata.
appdir="$(mktemp -d)/FlowDownload.AppDir"
mkdir -p "$appdir/usr/lib" "$appdir/usr/share/applications" "$appdir/usr/share/icons/hicolor/512x512/apps"
cp -R "$build" "$appdir/usr/lib/flowdownload"
cp "$here/AppRun" "$appdir/AppRun"
cp "$here/flowdownload.desktop" "$appdir/flowdownload.desktop"
cp "$here/flowdownload.desktop" "$appdir/usr/share/applications/flowdownload.desktop"
cp app/AppIcon.png "$appdir/flowdownload.png"
cp app/AppIcon.png "$appdir/usr/share/icons/hicolor/512x512/apps/flowdownload.png"
ln -s flowdownload.png "$appdir/.DirIcon"

ARCH=x86_64 appimagetool "$appdir" "dist/${name}.AppImage"
echo "==> wrote dist/${name}.AppImage and dist/${name}.tar.gz"
