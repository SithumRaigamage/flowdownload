#!/usr/bin/env bash
# build_app.sh — rebuild FlowDownload.app and (optionally) install it.
#
# Usage:
#   ./scripts/build_app.sh                  # just build; output lands in dist/
#   ./scripts/build_app.sh --version 1.1.0  # bump VERSION, then build with it
#   ./scripts/build_app.sh --install        # build + copy to ~/Applications
#   ./scripts/build_app.sh --install-system # build + copy to /Applications (needs sudo)
#   ./scripts/build_app.sh --launch         # build + open the app (lets you dock-pin it)
#
# The app version lives in the VERSION file at the project root (read by
# FlowDownload.spec and embedded as CFBundleShortVersionString/CFBundleVersion).
# Omitting --version re-embeds whatever's already in that file.
#
# The first build takes a minute or two; subsequent builds are much faster.

set -euo pipefail

# Run from the project root regardless of where the script is invoked from,
# so pyinstaller finds run.py and the spec file next to it.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR/.."

# Activate the venv the project ships with — pyinstaller lives there.
if [[ -f venv/bin/activate ]]; then
    # shellcheck disable=SC1091
    source venv/bin/activate
else
    echo "error: venv/bin/activate not found. Create the virtualenv first." >&2
    exit 1
fi

install_to=()
launch=false
new_version=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --install)        install_to=("$HOME/Applications") ;;
        --install-system) install_to=("/Applications") ;;
        --launch)         launch=true ;;
        --version)
            new_version="${2:-}"
            [[ -n "$new_version" ]] || { echo "error: --version requires a value, e.g. --version 1.1.0" >&2; exit 2; }
            shift
            ;;
        --help|-h)
            sed -n '2,13p' "$0"
            exit 0
            ;;
        *) echo "unknown flag: $1" >&2; exit 2 ;;
    esac
    shift
done

if [[ -n "$new_version" ]]; then
    if [[ ! "$new_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        echo "error: --version must look like X.Y.Z (got: $new_version)" >&2
        exit 2
    fi
    echo "$new_version" > VERSION
fi

echo "==> Building FlowDownload.app v$(cat VERSION) (this can take a minute)..."
pyinstaller FlowDownload.spec -y

APP_PATH="dist/FlowDownload.app"
if [[ ! -d "$APP_PATH" ]]; then
    echo "error: build did not produce $APP_PATH" >&2
    exit 1
fi

if [[ ${#install_to[@]} -gt 0 ]]; then
    dest="${install_to[0]}"
    mkdir -p "$dest"
    echo "==> Installing to $dest/FlowDownload.app"
    rm -rf "$dest/FlowDownload.app"
    cp -R "$APP_PATH" "$dest/FlowDownload.app"
    # Ad-hoc sign so Gatekeeper on Apple Silicon will run it. Without this,
    # double-clicking from Finder pops a "cannot be opened" dialog.
    codesign --force --deep --sign - "$dest/FlowDownload.app"
    echo "==> Installed."
fi

if $launch; then
    echo "==> Launching. Right-click the Dock icon and choose Options > Keep in Dock."
    open "${install_to[0]:-$APP_PATH}"
fi
