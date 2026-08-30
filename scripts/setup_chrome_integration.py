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


def register_native_host(extension_id: str) -> None:
    host_script = os.path.join(NATIVE_HOST_DIR, "flowdownload_host.py")
    os.chmod(host_script, 0o755)

    host_manifest = {
        "name": HOST_NAME,
        "description": "FlowDownload native messaging host",
        "path": host_script,
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }
    os.makedirs(CHROME_NATIVE_HOSTS_DIR, exist_ok=True)
    dest = os.path.join(CHROME_NATIVE_HOSTS_DIR, f"{HOST_NAME}.json")
    with open(dest, "w") as f:
        json.dump(host_manifest, f, indent=2)
        f.write("\n")
    print(f"Registered native messaging host: {dest}")


def main() -> None:
    if subprocess.run(["which", "openssl"], stdout=subprocess.DEVNULL).returncode != 0:
        print("openssl not found on PATH -- required to generate the extension key.", file=sys.stderr)
        sys.exit(1)

    pubkey_der = ensure_keypair()
    extension_id = compute_extension_id(pubkey_der)
    write_manifest_key(pubkey_der)
    register_native_host(extension_id)

    print()
    print("Setup complete. Extension ID (fixed, won't change on reload):")
    print(f"  {extension_id}")
    print()
    print("To finish, load the extension in Chrome:")
    print("  1. Open chrome://extensions")
    print("  2. Enable \"Developer mode\" (top right)")
    print("  3. Click \"Load unpacked\" and select:")
    print(f"     {EXTENSION_DIR}")
    print("  4. Right-click any link, image, or video and choose")
    print("     \"Download with FlowDownload\".")


if __name__ == "__main__":
    main()
