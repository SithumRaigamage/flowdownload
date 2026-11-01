# Roadmap

The goal is to bring FlowDownload up to the level of Internet Download Manager
(IDM) and the strongest open-source download managers, without giving up what
it already does well. Everything below is **not built yet**. It's split into
phases in the recommended order. Each entry has enough context to pick up cold:
what's missing, why it matters, and where it would go in the code.

Effort: **S** = a day or two, **M** = about a week, **L** = several weeks.

## Contents

- [Where FlowDownload stands today](#where-flowdownload-stands-today)
- [Feature comparison](#feature-comparison)
- [Phase 1 — Queues and limits](#phase-1--queues-and-limits)
- [Phase 2 — Capture](#phase-2--capture)
- [Phase 3 — Distribution and trust](#phase-3--distribution-and-trust)
- [Phase 4 — More protocols and power features](#phase-4--more-protocols-and-power-features)
- [Smaller known gaps](#smaller-known-gaps)
- [What we deliberately won't copy](#what-we-deliberately-wont-copy)
- [Sources](#sources)

## Where FlowDownload stands today

The download engine is already on par with IDM. IDM's headline feature,
"dynamic segmentation", splits a file while it downloads instead of fixing the
pieces up front. `_Run._steal()` in `app/engine/segmented.py` does the same
thing: a connection that runs out of work takes the back half of the slowest
unfinished piece.

Already built:

- Resume after dropped connections, with backoff when the server is busy, and
  recovery after quitting the app
- Multi-connection downloads with work stealing, and an optional connection
  limit per site
- Chrome capture that hands over cookies and the page Referer, plus a
  right-click "Download with FlowDownload"
- Refreshing an expired link, by hand or matched automatically from Chrome
- Duplicate handling (rename / skip / overwrite)
- SHA-256 of every finished file, checked against the server's `Digest` header
- A media Library with an in-app player (ffmpeg transcode for formats Qt
  can't play)
- A local API with live WebSocket updates, builds for macOS, Windows and Linux,
  and CI

The gaps are around the engine: scheduling and limits, ways of capturing
downloads, supported protocols, and distribution.

## Feature comparison

| Feature | IDM | AB DM | Gopeed | XDM | JDownloader | aria2 / Motrix / Persepolis | FlowDownload |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Multi-connection with dynamic splitting | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Speed limiter | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| Multiple queues + scheduler | ✅ | ✅ | – | ✅ | ✅ | ✅ (Persepolis) | ❌ one global queue |
| Categories → folders | ✅ | ✅ | – | ✅ | ✅ | – | 🟡 by file type only |
| Video / stream grabber (HLS, DASH) | ✅ | ✅ HLS | via extensions | ✅ | ✅ | – | ❌ |
| Clipboard monitoring | ✅ | – | – | ✅ | ✅ | – | ❌ |
| Batch / wildcard URLs, "download all links" | ✅ | ✅ | – | ✅ | ✅ LinkGrabber | – | ❌ |
| Firefox / Edge / Brave | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ Chrome only |
| Proxy / per-site logins | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ |
| FTP / BitTorrent / magnet | FTP | FTP | BT, magnet, ed2k | – | – | all | ❌ HTTP only |
| CLI / public API | CLI | CLI + API key | REST | – | – | JSON-RPC | 🟡 internal API, no CLI |
| Site grabber (crawl a site) | ✅ | – | – | – | 🟡 | – | ❌ |
| Plugins | – | – | ✅ JS | – | ✅ | – | ❌ |
| Signed builds + auto-update | ✅ | ✅ | ✅ | – | ✅ | ✅ | ❌ update check only |

---

## Phase 1 — Queues and limits

Why first: these are the features IDM users notice are missing straight away.
They only touch code we control (no browser or third-party changes), and they
fit the current engine.

### 1.1 Speed limiter — global cap built; per-download cap left — S
**Built:** an app-wide cap (`speed_limit_enabled` + `speed_limit_bps` in
`Settings`), enforced by one shared token bucket (`app/engine/throttle.py`)
that every chunk loop draws from: `DownloadWorker._stream_to_file` and
`_Run._fetch`. It has an on/off toggle in the dock, and the value lives in
Settings → Downloads.

**Left:**
- An optional per-download cap on `DownloadTask`: needs a DB column (a
  migration in `Storage._migrate`), a field in the detail sheet, and a second
  limiter per worker, consumed alongside the global one.
- Speed schedules, e.g. limit during working hours. This builds on 1.2.

### 1.2 Multiple queues and a scheduler — M
Today there's one global queue, managed by "Downloads at once" in
`DownloadManager._dispatch_queue` (`app/engine/manager.py`).

- Named queues, each with:
  - its own "downloads at once"
  - start and stop times, and the weekdays it runs
  - an action when it's finished: notify, sleep, shut down, or quit the app
- Storage: add a `queue_id` column to `downloads`, plus a `queues` table, as a
  migration in `Storage._migrate` (`app/engine/storage.py`).
- Manager: `_dispatch_queue` fills each queue up to its own limit, still within
  the per-site connection budget. A timer (the 1 s poll that already exists)
  starts and stops queues on their schedule.
- Waiting rows already show why they're waiting. Add the reason
  "queue paused until 02:00", alongside "site busy".
- Shutdown/sleep is platform-specific. Put it in `app/platform_util.py` and
  always show a cancellable countdown first.

### 1.3 Real categories — S
Today `auto_folder_by_type` sorts files into fixed `<type>/` folders.

- Replace it with editable categories: a name, file extensions, and a folder.
  Start with IDM's defaults: Compressed, Documents, Music, Programs, Video.
- `apply_auto_folder_for_path` in `app/engine/paths.py` looks up the matching
  category instead of a hard-coded table.
- Add a category filter to the Downloads view, next to the status filters.
- Migration: users who had `auto_folder_by_type` on get today's folders as
  their starting categories.

### 1.4 Proxy and per-site logins — S–M
- Proxy: follow the system proxy by default. Allow a manual HTTP or SOCKS5
  proxy in Settings (`requests[socks]`). Pass it from the shared request setup
  used by `DownloadWorker._get` and the probe functions.
- Per-site logins: save a username and password per host for HTTP Basic/Digest
  auth. Store the secret in the OS keychain (e.g. the `keyring` package), never
  in `settings.json`.
- Show a 401 with no saved login as "needs a login" with a button to add one,
  rather than as a generic failure.

### 1.5 More ways to verify files — built
Every finished file is checked, in this order, against: a checksum typed into
the Add download dialog (MD5/SHA-1/SHA-256, detected by length), the server's
RFC 9530 `Digest` header, or a published `file.sha256` / `SHA256SUMS` next to
the download (only for URLs without a query string, so signed links get no
extra requests). The result (`checksum_status`, `checksum_source`) shows in the
detail sheet; a mismatch keeps the file, marks it in red and raises a toast.
Code: `app/engine/checksum.py`, `DownloadWorker._check_integrity`.

---

## Phase 2 — Capture

Why next: this is where IDM feels like magic. Downloads get picked up wherever
they appear, not only when Chrome treats something as a download.

### 2.1 Video grabber — L
- Extension: watch responses with `chrome.webRequest` to spot media files and
  `.m3u8` (HLS) and `.mpd` (DASH) playlists on the current tab. Show a small
  "Download this video" button on the page (like IDM's floating bar), and list
  what was found in the popup.
- App: download HLS/DASH segments **natively**, with the existing worker, so
  pause/resume and retries keep working. Use the ffmpeg we already depend on
  (`app/media/transcode.py`) only to join the segments into one file at the end.
- Let the user pick a quality when the playlist offers several.
- Optional later: yt-dlp as an add-on for specific sites, kept off by default.
- Out of scope: encrypted or DRM-protected streams. Site-specific downloaders
  carry Terms of Service risk, so they shouldn't be on by default.

### 2.2 Clipboard monitoring — S
- Watch `QClipboard` for copied URLs whose extension is on a list in Settings
  (zip, exe, dmg, mp4, ...). Offer to add them through a notification or a
  prefilled Add sheet. Never add silently.
- Off by default.

### 2.3 Batch downloads and "download all links" — S–M
- Wildcard patterns in the Add sheet: `https://host/img[001-120].jpg`,
  `[a-z]`, with leading zeros kept. Preview the expanded list before adding.
- Extension: right-click → "Download all links with FlowDownload" collects a
  page's links, with filters by file type and by a pattern, then sends the
  selection over (similar to JDownloader's LinkGrabber).

### 2.4 More browsers — M
Today `app/chrome_setup.py` and `scripts/setup_chrome_integration.py` register
the native host only for stable Google Chrome (plus Chromium on Linux).

- Chromium-based browsers (Edge, Brave, Vivaldi, Opera, Chrome Beta/Canary):
  each reads its own native-messaging-hosts folder, in the same JSON format.
  Register in each one that's installed. Each browser still needs the extension
  loaded.
- Firefox: a near-identical native messaging manifest (it uses
  `allowed_extensions` instead of `allowed_origins`) and a few MV3 differences
  in the extension (`browser_specific_settings`, background scripts).
- Then publish to the Chrome Web Store (see 3.2) and Firefox Add-ons.

### 2.5 CLI and drag-and-drop — S
- `flowdownload add <url> [--queue NAME] [--dir PATH] [--referer URL]`, which
  talks to the running app through its local API (and starts it if it isn't
  running). Also `list`, `pause`, `resume`. This opens the app to scripts and
  tools like Raycast or Alfred.
- Accept links and `.txt` lists of links dropped onto the window or the Dock
  icon.

---

## Phase 3 — Distribution and trust

Why before Phase 4: unsigned builds and manual updates limit how many people
can install and keep using the app. Do this before trying to grow the user
base.

### 3.1 Code signing and notarization — M (needs paid developer accounts)
The `.app` isn't signed with an Apple Developer ID. That's fine on the machine
that built it (no quarantine flag), but anyone who downloads it gets
Gatekeeper's "unidentified developer" block. Signing needs an Apple Developer
Program membership, then a separate `xcrun notarytool` step. Windows needs an
Authenticode certificate to avoid SmartScreen warnings. Add both to
`.github/workflows/release.yml`.

### 3.2 Publish the extension to the stores — S
The extension is installed only as an unpacked Developer Mode extension.
Chrome may show "Disable developer mode extensions" warnings, and the extension
never updates itself: changes need a manual reload in `chrome://extensions`. A
reinstalled FlowDownload.app updates the files, but Chrome still needs that
reload. Publishing to the Chrome Web Store (and Firefox Add-ons, after 2.4)
fixes both. Keep the `key` in `manifest.json` so the extension ID stays the
same, because the native host manifest allows only that ID.

### 3.3 Auto-update for the app — M
Today the app checks for a new version (`/api/update-check`), but installing it
is manual. Two options:

- **Sparkle** (macOS) or the WinSparkle equivalent: a signed update feed. Needs
  3.1.
- **Simpler:** download the new build, check its SHA-256 against the release
  notes, swap it in, and restart.

Either way, downloads in progress resume after the restart, as they already do
after a normal quit.

---

## Phase 4 — More protocols and power features

Optional. Pick from this phase based on what users ask for.

### 4.1 FTP and SFTP — M
`ftplib` supports resuming with `REST`, and `paramiko` covers SFTP. Add a
protocol-specific worker behind the same `DownloadTask` model, and relax the
http(s)-only check in `validate_download_url` (`app/engine/probe.py`).

### 4.2 BitTorrent and magnet links — L
Use the `libtorrent` Python bindings as a separate engine behind the task
model. Needs:

- picking which files inside a torrent to download
- seeding limits (ratio and time)
- DHT/PEX
- opening `magnet:` links handed over by the browser

Doing this well is a lot of work. Gopeed and Motrix exist largely because of
it.

### 4.3 Site grabber — L
A crawler that starts from a URL, with crawl depth, same-domain-only, and file
type or pattern filters. It shows what it finds for review and can repeat on a
schedule (using 1.2) to pick up new files. This is IDM's least-used major
feature, so it comes last.

### 4.4 Mirrors, plugins and remote access — L
- **Mirrors / Metalink:** download one file from several URLs at the same time,
  with pieces from each, checked by size and hash.
- **Plugins:** a small JS or Python plugin system (like Gopeed's) for
  site-specific link resolving, so site-specific code stays out of the core.
- **Remote access:** an opt-in web UI on the local network, protected by a
  token, to add and watch downloads from a phone. The API already exists; this
  mainly needs auth and binding to a network address.

---

## Smaller known gaps

### No import of existing Chrome download history
The extension only sees downloads that start *after* it's loaded. Nothing pulls
in what's already in `chrome://downloads`.

### What the test suite doesn't cover
`tests/` (run with `venv/bin/python -m pytest`, and in CI) covers:

- the download engine, API and native host
- the extension's `background.js`, run under Node with a fake `chrome` API
  (`tests/chrome_harness.js`)
- the web UI, in an offscreen QtWebEngine page (`tests/ui/`)

All of it runs against a local `http.server` fixture. Not covered: the Qt
tray/window layer, an actual ffmpeg transcode (only its failure and caching
paths), the extension popup, and real Chrome. Each phase above should add tests
for what it builds. In particular, the speed limiter needs a test that checks
the total rate across a split download's connections.

## What we deliberately won't copy

- **Speed claims.** IDM advertises "up to 5×". Real gains depend on the server,
  and more connections can make things worse: trafficdeposit.com answered 503
  above about 12 parallel streams, which is why the per-site limit exists.
- **An Electron rewrite.** Motrix is criticised for its roughly 100 MB Electron
  runtime. The current PySide6 + web UI setup is lighter and works fine.
- **Built-in downloaders for DRM or specific sites.** These carry legal and
  Terms of Service risk. Keep them out of the core (see 2.1 and 4.4).

## Sources

- IDM: [About / features](https://mirror5.internetdownloadmanager.com/support/about.html),
  [feature page](https://cnet.internetdownloadmanager.com/features.html),
  [Site Grabber](https://mirror5.internetdownloadmanager.com/support/idm-grabber/index.html),
  [Softpedia review](https://www.softpedia.com/reviews/windows/Internet-Download-Manager-24164.shtml)
- AB Download Manager: [GitHub](https://github.com/amir1376/ab-download-manager),
  [1.7.0 release notes](https://www.neowin.net/amp/ab-download-manager-170/),
  [1.10.1 release notes](https://www.neowin.net/amp/ab-download-manager-1101/)
- Gopeed: [site](https://gopeed.com/), [docs](https://docs.gopeed.com),
  [review](https://dev.to/pickuma/gopeed-review-a-fast-scriptable-no-bloat-download-manager-for-developers-4e8l)
- XDM: [subhra74/xdm](https://github.com/subhra74/xdm)
- Persepolis (aria2 front end): [site](https://persepolisdm.github.io/),
  [man page](https://man.archlinux.org/man/persepolis.1.en)
- Overviews: [DEV — best download managers](https://dev.to/soroushpng/best-download-managers-in-2026-macos-windows-linux-opb),
  [GeekChamp — IDM alternatives](https://geekchamp.com/?p=247989)
