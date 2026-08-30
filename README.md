# FlowDownload

A resumable download manager for macOS with a Chrome extension that reroutes
Chrome's downloads through it, so a dropped connection means "retries
automatically," not "start over from zero."

## Running the app

FlowDownload is installed as a normal macOS app at
`/Applications/FlowDownload.app`. Double-click it, or add it to the Dock:

1. Open `/Applications` in Finder (or launch it once from Launchpad/Spotlight)
2. While it's running, right-click its Dock icon → **Options** → **Keep in
   Dock**

From then on it launches like any other app. Closing the window (the red
close button) quits the app entirely — that's the default. If you'd rather
it minimize to the system tray and keep downloading in the background
instead, there's a toggle for that in Settings.

The app keeps its database and settings in `~/.flowdownload/`.

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

### If you move or rebuild the project folder

`/Applications/FlowDownload.app` is just a thin launcher — its
`Contents/MacOS/FlowDownload` script points at this project's `venv` and
`run.py` by absolute path. If you move the project, update that script (or
re-run `scripts/setup_chrome_integration.py`, which also depends on the
project's path — see below) and re-copy the bundle:

```bash
cp -R dist/FlowDownload.app /Applications/FlowDownload.app
```

## Installing the Chrome extension

The extension and its native messaging host are already generated and
registered with Chrome — you only need to load the extension itself, since
Chrome's UI can't be scripted from the command line.

1. Open `chrome://extensions`
2. Turn on **Developer mode** (top right)
3. Click **Load unpacked** and select:
   ```
   /Users/sithumraigamage/projects/flowdownload/chrome_extension
   ```
4. That's it. From then on:
   - **Every download Chrome starts** gets cancelled instantly and handed to
     FlowDownload instead — this is what fixes flaky-connection failures.
   - **Right-click any link, image, video, or audio** → "Download with
     FlowDownload" also sends it over, for things Chrome wouldn't otherwise
     treat as a download.
   - FlowDownload launches automatically if it isn't already running.

`blob:`/`data:` URLs (page-generated content, e.g. some in-page "export"
buttons) aren't externally fetchable, so those are left to download through
Chrome normally — you'll still see them in Chrome's download bar.

### If you ever move the project folder

The native host registration and the extension's key both reference this
folder's current path. If you move it, re-run:

```bash
source venv/bin/activate
python scripts/setup_chrome_integration.py
```

and reload the extension in `chrome://extensions`.

### Uninstalling the Chrome integration

Remove the extension from `chrome://extensions`, then delete the native
messaging registration:

```bash
rm ~/"Library/Application Support/Google/Chrome/NativeMessagingHosts/com.flowdownload.host.json"
```

## Troubleshooting

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
  (`pgrep -fl run.py`), then delete the pid file.
- **App won't quit / `Ctrl+C` in a terminal doesn't stop it** — should not
  happen anymore (fixed by adding a proper `SIGINT` handler); if you still
  see it, force-quit with `pkill -9 -f run.py`.

## Roadmap

Known gaps and possible future work (platform support beyond macOS, code
signing, bandwidth throttling, an actual automated test suite, etc.) are
tracked in [docs/roadmap.md](docs/roadmap.md).
