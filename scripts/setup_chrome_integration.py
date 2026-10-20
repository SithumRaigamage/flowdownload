#!/usr/bin/env python3
"""One-time setup for the FlowDownload Chrome integration.

Generates a stable RSA keypair for the extension so its ID is deterministic
(computed from the public key, the same way Chrome itself derives IDs from a
manifest "key" field), writes that key into chrome_extension/manifest.json,
and registers the native messaging host manifest with Chrome so the
extension can talk to native_host/flowdownload_host.py.

Doing it this way (vs. leaving the ID to be assigned when the unpacked
extension is loaded) avoids a manual round-trip: without a fixed key, the
ID depends on the exact filesystem path Chrome loads the extension from,
so the native host's allowed_origins would have to be updated by hand
after loading it once. Precomputing lets everything be registered in one
shot, before the extension is ever loaded.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import chrome_setup  # noqa: E402
from app.platform_util import IS_MAC, IS_WINDOWS  # noqa: E402

EXTENSION_DIR = os.path.join(PROJECT_ROOT, "chrome_extension")
NATIVE_HOST_DIR = os.path.join(PROJECT_ROOT, "native_host")
KEY_DIR = os.path.join(os.path.expanduser("~"), ".flowdownload")
PRIVATE_KEY_PATH = os.path.join(KEY_DIR, "chrome_extension_key.pem")
PUBLIC_KEY_DER_PATH = os.path.join(KEY_DIR, "chrome_extension_key_pub.der")



def run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def ensure_keypair() -> bytes:
    os.makedirs(KEY_DIR, exist_ok=True)
    if not os.path.exists(PRIVATE_KEY_PATH):
        run(["openssl", "genrsa", "-out", PRIVATE_KEY_PATH, "2048"])
        print(f"Generated extension signing key: {PRIVATE_KEY_PATH}")
    else:
        print(f"Reusing existing extension signing key: {PRIVATE_KEY_PATH}")

    run(["openssl", "rsa", "-in", PRIVATE_KEY_PATH, "-pubout",
         "-outform", "DER", "-out", PUBLIC_KEY_DER_PATH])
    with open(PUBLIC_KEY_DER_PATH, "rb") as f:
        return f.read()


def compute_extension_id(pubkey_der: bytes) -> str:
    return chrome_setup.extension_id_from_key(pubkey_der)


def write_manifest_key(pubkey_der: bytes) -> None:
    manifest_path = os.path.join(EXTENSION_DIR, "manifest.json")
    with open(manifest_path) as f:
        manifest = json.load(f)
    manifest["key"] = base64.b64encode(pubkey_der).decode()
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"Wrote signing key into {manifest_path}")


# Installed macOS bundles. Packaged Windows/Linux builds register their own
# host on every launch (app/chrome_setup.register_installed_app), so on those
# this script is only for source checkouts.
APP_BUNDLE_CANDIDATES = (
    "/Applications/FlowDownload.app",
    os.path.join(os.path.expanduser("~"), "Applications", "FlowDownload.app"),
) if IS_MAC else ()
BUNDLED_HOST = os.path.join("Contents", "MacOS", "flowdownload-host")
SOURCE_LAUNCHER = os.path.join(KEY_DIR, "flowdownload-host.cmd" if IS_WINDOWS else "flowdownload-host")


def host_executable(app_bundle: str | None) -> str:
    """What Chrome should run: the host inside an installed FlowDownload.app
    if there is one (self-contained -- no checkout or venv needed), else a
    launcher for this source checkout."""
    bundles = [app_bundle] if app_bundle else APP_BUNDLE_CANDIDATES
    for bundle in bundles:
        candidate = os.path.join(bundle, BUNDLED_HOST)
        if os.access(candidate, os.X_OK):
            return candidate
    if app_bundle:
        sys.exit(f"{app_bundle} has no {BUNDLED_HOST} -- rebuild it with scripts/build_app.sh")
    # Pin this interpreter (the one with the dependencies installed) rather
    # than relying on the script's shebang, which used to hard-code one
    # machine's venv path.
    host_script = os.path.join(NATIVE_HOST_DIR, "flowdownload_host.py")
    os.makedirs(KEY_DIR, exist_ok=True)
    with open(SOURCE_LAUNCHER, "w") as f:
        if IS_WINDOWS:
            f.write(f'@echo off\r\n"{sys.executable}" "{host_script}" %*\r\n')
        else:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" "{host_script}" "$@"\n')
    os.chmod(SOURCE_LAUNCHER, 0o755)
    return SOURCE_LAUNCHER


def register_native_host(extension_id: str, host_path: str) -> None:
    for dest in chrome_setup.register(extension_id, host_path):
        print(f"Registered native messaging host: {dest}")
    print(f"  -> {host_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", help="macOS: register the host bundled in this FlowDownload.app "
                        "(default: an installed one in /Applications or ~/Applications, else this checkout)")
    args = parser.parse_args()

    if shutil.which("openssl") is None:
        print("openssl not found on PATH -- required to generate the extension key.", file=sys.stderr)
        sys.exit(1)

    pubkey_der = ensure_keypair()
    extension_id = compute_extension_id(pubkey_der)
    write_manifest_key(pubkey_der)
    host_path = host_executable(args.app)
    register_native_host(extension_id, host_path)
    extension_dir = EXTENSION_DIR
    if host_path.endswith(BUNDLED_HOST):
        # Load the copy shipped in the app too, so nothing depends on this
        # checkout -- and reinstalling the app updates the extension with it.
        bundle = host_path[: -len(BUNDLED_HOST)]
        extension_dir = os.path.join(bundle, "Contents", "Resources", "chrome_extension")

    print()
    print("Setup complete. Extension ID (fixed, won't change on reload):")
    print(f"  {extension_id}")
    print()
    print("To finish, load the extension in Chrome:")
    print("  1. Open chrome://extensions")
    print("  2. Enable \"Developer mode\" (top right)")
    print("  3. Click \"Load unpacked\" and select:")
    print(f"     {extension_dir}")
    print("  4. Right-click any link, image, or video and choose")
    print("     \"Download with FlowDownload\".")


if __name__ == "__main__":
    main()
