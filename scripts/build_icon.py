"""Generate app/AppIcon.icns from the in-app icon renderer.

Run from the project root (with venv activated) after editing
`app/ui/app_icon.py` to update both the runtime icon and the bundle icons
(.icns for macOS, plus .ico for Windows and .png for Linux):

    python scripts/build_icon.py

Requires Pillow for the multi-size .icns assembly (it ships its own
icns writer via PIL.IcnsImage). The PIL pillow-icns feature is enabled
in the Pillow that ships with PySide6's dependency set; if not, fall
back to iconutil — see _build_icns_with_iconutil below.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Make the `app` package importable when this script is run directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.ui.app_icon import make_dock_icon  # noqa: E402

ICON_DIR = ROOT / "app" / "AppIcon.iconset"
ICNS_PATH = ROOT / "app" / "AppIcon.icns"
ICO_PATH = ROOT / "app" / "AppIcon.ico"   # Windows exe + installer icon
PNG_PATH = ROOT / "app" / "AppIcon.png"   # Linux .desktop / AppImage icon
ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]

# macOS .icns expects the icon at all of these pixel sizes so Finder, the
# Dock, Launchpad, and the app switcher can each grab the right one.
SIZES = [16, 32, 64, 128, 256, 512, 1024]


def _save_pixmap(pixmap, path: Path) -> None:
    pixmap.save(str(path), "PNG")


def _build_iconset() -> Path:
    _app = QApplication.instance() or QApplication(sys.argv)  # QPixmap needs one alive
    ICON_DIR.mkdir(parents=True, exist_ok=True)

    # iconset filenames follow a strict convention: <size> or <size>@2x.
    # The @2x variants are the Retina sizes; a 512x512@2x.png is the
    # 1024x1024 source artwork.
    for size in SIZES:
        pix = make_dock_icon(size)
        name = f"icon_{size}x{size}.png"
        _save_pixmap(pix, ICON_DIR / name)

    # @2x variants: a 32x32@2x is 64x64, etc.
    pairs = [(16, 32), (32, 64), (64, 128), (128, 256), (256, 512), (512, 1024)]
    for base, retina in pairs:
        pix = make_dock_icon(retina)
        _save_pixmap(pix, ICON_DIR / f"icon_{base}x{base}@2x.png")

    return ICON_DIR


def _build_icns_with_iconutil(iconset: Path, out: Path) -> None:
    """iconutil is the canonical macOS tool for building .icns from an
    iconset. It is bundled with Xcode command-line tools, so it's already
    on the machine of anyone who builds a Qt app on macOS."""
    if shutil.which("iconutil") is None:
        raise SystemExit(
            "iconutil not found. Install Xcode Command Line Tools "
            "(`xcode-select --install`) and rerun."
        )
    if out.exists():
        out.unlink()
    subprocess.run(
        ["iconutil", "-c", "icns", str(iconset), "-o", str(out)],
        check=True,
    )


def _png_bytes(size: int) -> bytes:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice

    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    make_dock_icon(size).save(buf, "PNG")
    buf.close()
    return bytes(data)


def _build_ico(out: Path) -> None:
    """A multi-size .ico. Every entry is stored as PNG (allowed since
    Windows Vista), so the file is just a directory in front of the PNGs --
    no Pillow needed."""
    import struct

    images = [(size, _png_bytes(size)) for size in ICO_SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))  # reserved, type=icon, count
    offset = len(header) + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO directory
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    out.write_bytes(header + entries + blobs)


def build_windows_linux_icons() -> None:
    QApplication.instance() or QApplication(sys.argv)
    _build_ico(ICO_PATH)
    PNG_PATH.write_bytes(_png_bytes(512))
    print(f"wrote {ICO_PATH} and {PNG_PATH}")


def main() -> None:
    build_windows_linux_icons()
    if sys.platform != "darwin":
        return  # .icns needs macOS's iconutil

    iconset = _build_iconset()
    print(f"iconset built at {iconset}")

    # Use a temp dir for iconutil's output so a partial run doesn't leave a
    # half-written .icns on disk.
    with tempfile.TemporaryDirectory() as tmp:
        tmp_icns = Path(tmp) / "AppIcon.icns"
        _build_icns_with_iconutil(iconset, tmp_icns)
        if ICNS_PATH.exists():
            ICNS_PATH.unlink()
        shutil.copy2(tmp_icns, ICNS_PATH)

    print(f"wrote {ICNS_PATH}")


if __name__ == "__main__":
    main()
