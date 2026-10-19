from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field


def _default_download_dir() -> str:
    return os.path.join(os.path.expanduser("~"), "Downloads")


@dataclass
class Settings:
    download_dir: str = field(default_factory=_default_download_dir)
    max_concurrent_downloads: int = 3
    notifications_enabled: bool = True
    minimize_to_tray: bool = False
    # When True, new downloads are routed to <download_dir>/<type>/<file>
    # based on extension. Off by default to preserve existing user behavior.
    auto_folder_by_type: bool = False
    # What to do when a URL matches a completed download: "rename" keeps
    # both files (today's default -- a new filename is picked automatically),
    # "skip" reuses the existing completed task instead of downloading again,
    # "overwrite" replaces the existing file with a fresh copy.
    duplicate_policy: str = "rename"
    # Connections a single large download may split across (1 = never split).
    # Drawn from the per-site budget, so more per file means fewer files at
    # once from the same site.
    connections_per_download: int = 4
    # Optional connection budget per site (domain_key, so v1.cdnde.com and
    # v2.cdnde.com share one), on top of max_concurrent_downloads. 0 = off,
    # so "Downloads at once" alone decides. Set it for CDNs that reset or
    # stall under many parallel streams (trafficdeposit.com answered 503
    # above ~12). A split-file download uses several connections from it.
    site_connection_limit: int = 0
    # "system" follows macOS's light/dark appearance; "light"/"dark" pin it.
    # Kept here (not just in the page) so the native window and title bar
    # can match the page from the moment the app starts.
    theme: str = "system"

    @classmethod
    def load(cls, path: str) -> Settings:
        if not os.path.exists(path):
            return cls()
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return cls()
        if not isinstance(data, dict):
            return cls()
        defaults = cls()
        known = {}
        for key, value in data.items():
            if key not in cls.__dataclass_fields__:
                continue
            # A hand-edited or corrupted value of the wrong type (e.g.
            # "3" for max_concurrent_downloads) would crash the dispatcher
            # later; fall back to that one setting's default instead.
            expected = type(getattr(defaults, key))
            if expected is int and isinstance(value, bool):
                continue
            if isinstance(value, expected):
                known[key] = value
        return cls(**known)

    def save(self, path: str) -> None:
        # tmp + os.replace: a crash mid-write used to leave truncated JSON,
        # which load() then silently replaced with all defaults.
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(asdict(self), f, indent=2)
        os.replace(tmp, path)
