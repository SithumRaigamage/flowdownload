# Roadmap / known gaps

Everything below is **not built yet**. None of it was asked for — this is a
backlog of things that came up as known edges while building the app, kept
here so they don't have to be rediscovered later. Each entry has enough
context to pick up cold.

## Platform support

### macOS only
Every OS touchpoint is Mac-specific:
- `app/main.py`, `native_host/flowdownload_host.py` — app data dir
  (`~/.flowdownload/`) is fine cross-platform, but...
- `scripts/setup_chrome_integration.py` — registers the native messaging
  host at `~/Library/Application Support/Google/Chrome/NativeMessagingHosts/`,
  a macOS-only path (Windows uses a registry key; Linux uses
  `~/.config/google-chrome/NativeMessagingHosts/`)
- `dist/FlowDownload.app` — the whole `.app` bundle structure
  (`Info.plist`, `Contents/MacOS/...`) is macOS-only; Windows would need an
  `.exe` + `.ico` (e.g. via PyInstaller), Linux a `.desktop` file
- `app/ui/tray.py` — `QSystemTrayIcon` itself is cross-platform (Qt), so
  this one mostly just works elsewhere already

To port: keep the engine layer (`app/engine/`) as-is — it's plain Python,
no macOS dependencies — and add a platform-specific packaging + native-host
registration path per OS.

### Only Google Chrome (stable channel)
`scripts/setup_chrome_integration.py` registers the native host for the
default Chrome install only. Brave, Edge, Chrome Beta/Canary, and other
Chromium-based browsers each read their *own* native messaging hosts
directory (usually named after the browser but same JSON format) and would
each need the same manifest copied there, plus their own "load unpacked
extension" step.

## Distribution

### No code signing / notarization
The `.app` isn't signed with an Apple Developer ID. Fine for local use on
this machine (no Gatekeeper quarantine flag gets attached), but sharing it
with anyone else — or moving it via a method that adds
`com.apple.quarantine` (e.g. downloading it from a link) — would trigger
Gatekeeper's "unidentified developer" block. Signing requires an Apple
Developer Program membership; notarization is a further `xcrun notarytool`
step after that.

### Extension is Developer Mode / unpacked only
Not published to the Chrome Web Store. Works fine long-term for personal
use, but: it only exists on this machine, Chrome may occasionally show a
"Disable developer mode extensions" style nag depending on Chrome's current
policy, and there's no auto-update mechanism for it — changes require
reloading it manually from `chrome://extensions`. (Loaded from the copy
inside an installed FlowDownload.app, reinstalling the app updates the files;
Chrome still needs that reload to pick them up.)

### No auto-update for the app itself
New builds require manually re-copying `dist/FlowDownload.app` to
`/Applications`. A real updater would mean either a signed-and-notarized
build (Sparkle-style update feed) or something much simpler like a
version-check-and-prompt-to-redownload flow.

## App features

### Checksums only from the server's `Digest` header
`DownloadWorker` hashes every finished file (SHA-256) and verifies it
against an RFC 9530 `Digest` header when the server sends one. Not covered:
the sidecar-file convention (`file.zip.sha256` next to the download), or a
user-supplied checksum typed into the Add download dialog.

### No bandwidth throttling
`app/engine/downloader.py` streams as fast as the connection allows — no
setting to cap a download's (or the app's aggregate) speed. Would live
as a `max_bandwidth_bps` in `Settings`, enforced with a token-bucket sleep
in `DownloadWorker._stream_to_file`'s chunk loop.

### No import of existing Chrome download history
The extension only sees downloads that start *after* it's loaded — nothing
retroactively pulls in what's already in `chrome://downloads`.

## Testing

### What the test suite doesn't cover
`tests/` (run with `venv/bin/python -m pytest`, and in CI) exercises the
download engine, API, native host, the extension's `background.js` (under
Node with a fake `chrome` API, `tests/chrome_harness.js`) and the web UI
(offscreen QtWebEngine, `tests/ui/`) against a local `http.server` fixture.
Not covered: the Qt tray/window layer, an actual ffmpeg transcode (only its
failure and caching paths), the extension popup, and real Chrome.
