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
import hashlib
import json
import os
import subprocess
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXTENSION_DIR = os.path.join(PROJECT_ROOT, "chrome_extension")
NATIVE_HOST_DIR = os.path.join(PROJECT_ROOT, "native_host")
KEY_DIR = os.path.join(os.path.expanduser("~"), ".flowdownload")
PRIVATE_KEY_PATH = os.path.join(KEY_DIR, "chrome_extension_key.pem")
PUBLIC_KEY_DER_PATH = os.path.join(KEY_DIR, "chrome_extension_key_pub.der")

HOST_NAME = "com.flowdownload.host"
CHROME_NATIVE_HOSTS_DIR = os.path.join(
    os.path.expanduser("~"), "Library", "Application Support",
    "Google", "Chrome", "NativeMessagingHosts",
)


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
    digest = hashlib.sha256(pubkey_der).digest()[:16]
    return "".join(chr(ord("a") + (b >> 4)) + chr(ord("a") + (b & 0xF)) for b in digest)


def write_manifest_key(pubkey_der: bytes) -> None:
    manifest_path = os.path.join(EXTENSION_DIR, "manifest.json")
    with open(manifest_path) as f:
        manifest = json.load(f)
    manifest["key"] = base64.b64encode(pubkey_der).decode()
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"Wrote signing key into {manifest_path}")


APP_BUNDLE_CANDIDATES = (
    "/Applications/FlowDownload.app",
    os.path.join(os.path.expanduser("~"), "Applications", "FlowDownload.app"),
)
BUNDLED_HOST = os.path.join("Contents", "MacOS", "flowdownload-host")
SOURCE_LAUNCHER = os.path.join(KEY_DIR, "flowdownload-host")


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
        f.write(f'#!/bin/sh\nexec "{sys.executable}" "{host_script}" "$@"\n')
    os.chmod(SOURCE_LAUNCHER, 0o755)
    return SOURCE_LAUNCHER


def register_native_host(extension_id: str, host_path: str) -> None:
    host_manifest = {
        "name": HOST_NAME,
        "description": "FlowDownload native messaging host",
        "path": host_path,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }
    os.makedirs(CHROME_NATIVE_HOSTS_DIR, exist_ok=True)
    dest = os.path.join(CHROME_NATIVE_HOSTS_DIR, f"{HOST_NAME}.json")
    with open(dest, "w") as f:
        json.dump(host_manifest, f, indent=2)
        f.write("\n")
    print(f"Registered native messaging host: {dest}")
    print(f"  -> {host_path}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--app", help="register the host bundled in this FlowDownload.app "
                        "(default: an installed one in /Applications or ~/Applications, else this checkout)")
    args = parser.parse_args()

    if subprocess.run(["which", "openssl"], stdout=subprocess.DEVNULL).returncode != 0:
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
