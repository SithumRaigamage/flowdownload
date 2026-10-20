# FlowDownload

A resumable download manager for macOS, Windows and Linux with a Chrome extension that reroutes
Chrome's downloads through it, so a dropped connection means "retries
automatically," not "start over from zero."

## Download

**[Get the latest release →](https://github.com/SithumRaigamage/flowdownload/releases/latest)**

| OS | File |
|---|---|
| macOS (Apple Silicon / Intel) | `.dmg` (`macos-arm64` / `macos-x64`) |
| Windows 10/11 | `-windows-x64-setup.exe` |
| Linux | `.AppImage` or `.tar.gz` |
| Chrome extension | `-chrome-extension.zip` |

See **[docs/INSTALL.md](docs/INSTALL.md)** for step-by-step install
instructions, including the first-launch warning (the builds aren't
code-signed yet) and setting up the Chrome extension. A packaged build
registers itself with Chrome on launch, so no setup script is needed.

Maintainers: see [docs/RELEASING.md](docs/RELEASING.md) to publish a new version.

## Building from source (macOS)

FlowDownload is a PyInstaller-built app. On macOS, build it with:

```bash
./scripts/build_app.sh                  # build into dist/FlowDownload.app
./scripts/build_app.sh --version 1.2.0  # bump VERSION first, then build
./scripts/build_app.sh --install        # build + copy to ~/Applications
./scripts/build_app.sh --install-system # build + copy to /Applications (needs sudo)
./scripts/build_app.sh --launch         # build + open it
```

Then double-click `FlowDownload.app` (or the installed copy). To keep it in
the Dock: while it's running, right-click its Dock icon → **Options** →
**Keep in Dock**.

**Quit the app before rebuilding** if you run it straight from `dist/` — the
build replaces that folder in place. Your downloads aren't lost: anything
mid-download when the app quits resumes automatically on the next launch.

Closing the window (the red close button) quits the app entirely — that's
the default. If you'd rather it minimize to the system tray and keep
downloading in the background instead, there's a toggle for that in Settings.

The app keeps its database and settings in `~/.flowdownload/`.

### Version

The version lives in the `VERSION` file at the project root. It's embedded
in the app bundle's Info.plist and shown in the app itself (top right of the
window and Settings → About).

### Running from source instead

Useful while developing, or if you want to see stdout/stderr directly:

```bash
cd /Users/sithumraigamage/projects/flowdownload
source venv/bin/activate
python run.py
```

Dependencies are already installed in `venv/`. To reinstall them:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Tests

```bash
pip install -r requirements-dev.txt   # once
python -m pytest                      # ~70 s, local server only
```

`requirements.lock` pins the exact versions CI tests against
(`.github/workflows/ci.yml`); `pip install -r requirements.lock` reproduces
that environment.

## How downloads behave

- **Dropped connections retry automatically** and resume from where they
  stopped (when the server supports byte ranges). A connection that made
  progress resets the retry counter, so a flaky but working download is never
  marked failed; one that gets nowhere gives up after 30 attempts.
- **Large files are split across several connections** (Settings →
  Downloads → "Connections per download", default 4; 1 turns it off) when
  the file is 8 MB or more and the server supports resumable ranges. Progress
  per piece is kept in a `<file>.part.segments` file next to the `.part`, so
  split downloads resume after a pause or restart too.
- **"Downloads at once" decides how many files run.** An optional per-site
  cap (Settings → Downloads → "Connections per site", 0 = off by default;
  every `*.trafficdeposit.com` host counts as one site) can be set for CDNs
  that answer 503 under many parallel streams (about 12 for
  trafficdeposit.com). When it's set, a split download uses several of its
  connections, and extra downloads wait in the queue with the reason shown.
- **Chrome's page address is sent as the Referer**, since many CDNs refuse
  or throttle downloads without one.
- **Expired links can be refreshed**: right-click an unfinished download →
  **Refresh link…** and paste a new URL; downloaded data is kept when the
  server still has the same file. Starting the same download again from
  Chrome does this automatically when it clearly matches a failed or paused
  download (same site, and the same filename, ETag or size).
- **Quitting doesn't lose progress**: interrupted downloads resume on the
  next launch. Downloads you paused yourself stay paused.
- **Cancel** (right-click, or the detail panel) stops a download and deletes
  its partial file. **Remove** takes it off the list and keeps a completed
  file; for an unfinished one it asks first, since its partial data is
  discarded. **Delete file…** (right-click a completed download) removes it
  from disk too.
- **In-app preview** of video/audio needs `ffmpeg` (`brew install ffmpeg`)
  for formats Qt's built-in player can't decode, such as H.264/AAC; without
  it the player says so instead of loading forever.

## Installing the Chrome extension

Using a downloaded release? Follow [docs/INSTALL.md](docs/INSTALL.md#chrome-extension-optional)
instead. The steps below are for a source checkout.

First register the native messaging host (the small program Chrome hands
downloads to):

```bash
source venv/bin/activate
python scripts/setup_chrome_integration.py   # or: --app /path/to/FlowDownload.app
```

If FlowDownload.app is installed in `/Applications` or `~/Applications`, this
registers the host bundled inside it, so the integration no longer depends on
this source folder or its venv. Otherwise it registers a launcher for this
checkout. Either way it prints the folder to load in the next step; Chrome's
UI can't be scripted from the command line.

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. Click **Load unpacked** and select the folder the setup script printed:
   `FlowDownload.app/Contents/Resources/chrome_extension` for an installed
   app, or this checkout's `chrome_extension/`
4. That's it. From then on:
   - **Every download Chrome starts** is paused instantly and handed to
     FlowDownload instead — this is what fixes flaky-connection failures.
     Chrome's copy is removed only once FlowDownload confirms it queued the
     download; if the hand-off fails, it carries on in Chrome.
   - The extension's popup has a switch to stop sending downloads, and a
     list of sites whose downloads should stay in Chrome (useful for
     downloads that only work in the browser, e.g. ones started by
     submitting a form).
   - **Right-click any link, image, video, or audio** → "Download with
     FlowDownload" also sends it over, for things Chrome wouldn't otherwise
     treat as a download.
   - FlowDownload launches automatically if it isn't already running.

`blob:`/`data:` URLs (page-generated content, e.g. some in-page "export"
buttons) aren't externally fetchable, so those are left to download through
Chrome normally — you'll still see them in Chrome's download bar.

### If you ever move the project folder (or the app)

When registered against this checkout rather than an installed app, the
native host registration references this folder's current path. If you move
it, or install the app after the fact, re-run:

```bash
source venv/bin/activate
python scripts/setup_chrome_integration.py
```

and reload the extension in `chrome://extensions`.

### Uninstalling the Chrome integration

Remove the extension from `chrome://extensions`, then delete the native
messaging registration:

```bash
# macOS
rm ~/"Library/Application Support/Google/Chrome/NativeMessagingHosts/com.flowdownload.host.json"
# Linux
rm ~/.config/{google-chrome,chromium}/NativeMessagingHosts/com.flowdownload.host.json
```

On Windows, uninstalling FlowDownload removes the registration.

## Troubleshooting

- **Logs**: when launched from Finder or the Dock, the app writes its
  diagnostics to `~/.flowdownload/flowdownload.log` (the previous one is
  kept as `.log.1`). Run from a terminal, they go to the terminal instead.

- **"Download with FlowDownload" does nothing / extension shows an error** —
  make sure FlowDownload has been launched at least once so
  `~/.flowdownload/` exists, then reload the extension.
- **A download fails immediately with a 401/403** — the site likely needs a
  cookie the extension didn't have (e.g. you weren't logged in in that
  browser profile), or blocks non-browser requests outright for that
  specific URL.
- **Two copies of the app end up running** — shouldn't happen; FlowDownload
  uses a lock file (`~/.flowdownload/app.pid`) to refuse a second launch. If
  it ever gets stuck: quit FlowDownload (from its window, or the tray icon if
  minimize-to-tray is on), confirm nothing is left running
  (`pgrep -fl FlowDownload`), then delete the pid file.
- **App won't quit / `Ctrl+C` in a terminal doesn't stop it** — should not
  happen anymore (fixed by adding a proper `SIGINT` handler); if you still
  see it, force-quit with `pkill -9 -f FlowDownload.app` (or `pkill -9 -f run.py`
  when running from source).

## Roadmap

Known gaps and possible future work (code signing, bandwidth throttling,
etc.) are
tracked in [docs/roadmap.md](docs/roadmap.md).

