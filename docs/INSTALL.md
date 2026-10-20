# Installing FlowDownload

Download the latest version from the
**[Releases page](https://github.com/SithumRaigamage/flowdownload/releases/latest)**.
Under **Assets**, pick the file for your computer:

| Your computer | Download this file |
|---|---|
| Mac with Apple Silicon (M1, M2, M3, M4 …) | `FlowDownload-<version>-macos-arm64.dmg` |
| Mac with an Intel processor | `FlowDownload-<version>-macos-x64.dmg` |
| Windows 10 / 11 (64-bit) | `FlowDownload-<version>-windows-x64-setup.exe` |
| Linux (64-bit, most distros) | `FlowDownload-<version>-linux-x86_64.AppImage` |
| Linux, if you'd rather not use AppImage | `FlowDownload-<version>-linux-x86_64.tar.gz` |
| Chrome extension (any OS, optional) | `FlowDownload-<version>-chrome-extension.zip` |

Not sure which Mac you have? Click the Apple menu → **About This Mac**. If
it says **Chip: Apple M…**, use `arm64`. If it says **Processor: Intel**,
use `x64`.

> These builds aren't signed with a paid Apple or Microsoft certificate yet,
> so the first launch shows a warning. The steps below explain how to get
> past it. `SHA256SUMS.txt` on the release page lets you verify the download
> if you want to.

---

## macOS

1. Open the `.dmg` and drag **FlowDownload** onto the **Applications** folder.
2. The first time, open **Applications**, **right-click** FlowDownload, choose
   **Open**, then **Open** again in the dialog. After that it opens normally.
   - On macOS 15 (Sequoia) and later, if no **Open** button appears: try to
     open the app once, then go to **System Settings → Privacy & Security**,
     scroll down, and click **Open Anyway**.
   - Or, in Terminal: `xattr -dr com.apple.quarantine /Applications/FlowDownload.app`
3. Optional: to keep it in the Dock, right-click its Dock icon while it's
   running → **Options → Keep in Dock**.

## Windows

1. Run `FlowDownload-<version>-windows-x64-setup.exe`.
2. If Windows SmartScreen says *"Windows protected your PC"*, click
   **More info → Run anyway**.
3. Follow the installer. It installs for your user only, so no admin
   password is needed. FlowDownload then appears in the Start menu.

To uninstall, use **Settings → Apps → Installed apps → FlowDownload →
Uninstall**.

## Linux

**AppImage (recommended):**

```bash
chmod +x FlowDownload-*-linux-x86_64.AppImage
./FlowDownload-*-linux-x86_64.AppImage
```

If it says something about FUSE (common on Ubuntu 22.04 and later), install
it with `sudo apt install libfuse2` (on Ubuntu 24.04 the package is
`libfuse2t64`). Or run it without FUSE:
`./FlowDownload-*.AppImage --appimage-extract-and-run`.

Put the AppImage somewhere permanent, such as `~/Applications`, before you
set up the Chrome extension. The extension finds the app wherever it was
last started from.

**Tarball:**

```bash
tar xzf FlowDownload-*-linux-x86_64.tar.gz
./FlowDownload/FlowDownload
```

If the window doesn't open, install the X11/Qt runtime libraries your distro
may be missing. On Debian/Ubuntu:
`sudo apt install libxcb-cursor0 libxkbcommon-x11-0 libegl1 libnss3`.

The **Choose folder** button uses `zenity` (GNOME) or `kdialog` (KDE). Most
desktops already have one of them.

---

## Chrome extension (optional)

The extension adds **Download with FlowDownload** to Chrome's right-click
menu and can hand Chrome's own downloads to the app.

1. Install and **start FlowDownload once**. On first launch it registers
   itself with Chrome automatically. There's nothing to run.
2. Unzip `FlowDownload-<version>-chrome-extension.zip` somewhere permanent,
   because Chrome loads it from that folder each time it starts.
3. Open `chrome://extensions`, turn on **Developer mode** (top right), click
   **Load unpacked**, and select the unzipped `chrome_extension` folder.
4. Right-click any link, image or video → **Download with FlowDownload**.

FlowDownload's **Settings → Chrome** page shows whether the integration
is working.

## Preview playback (optional)

FlowDownload can preview videos in the app. Some formats (H.264/AAC MP4,
for example) need **ffmpeg** for that:

- macOS: `brew install ffmpeg`
- Windows: `winget install ffmpeg`
- Linux: `sudo apt install ffmpeg` (or your distro's equivalent)

Everything else works without it.

## Where your data lives

Downloads go to the folder you choose in Settings. The app's own database,
settings and log are in `~/.flowdownload/` (on Windows,
`C:\Users\<you>\.flowdownload\`). Uninstalling the app leaves them in
place.
