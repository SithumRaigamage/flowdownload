#!/usr/bin/env bash
# make_dmg.sh — wrap dist/FlowDownload.app into a drag-to-Applications DMG.
#
# Usage (from the project root, after `pyinstaller FlowDownload.spec`):
#   packaging/macos/make_dmg.sh <output.dmg>
#
# The app is ad-hoc signed only (no Apple Developer ID), so the first launch
# needs right-click > Open; see docs/INSTALL.md.

set -euo pipefail

out="${1:?usage: make_dmg.sh <output.dmg>}"
app="dist/FlowDownload.app"
[[ -d "$app" ]] || { echo "error: $app not found -- run pyinstaller first" >&2; exit 1; }

# Apple Silicon refuses to run unsigned code at all; an ad-hoc signature is
# enough for it to run once Gatekeeper is told to allow it.
codesign --force --deep --sign - "$app"

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT
cp -R "$app" "$staging/"
ln -s /Applications "$staging/Applications"

rm -f "$out"
hdiutil create -volname "FlowDownload" -srcfolder "$staging" -ov -format UDZO "$out"
echo "==> wrote $out"
