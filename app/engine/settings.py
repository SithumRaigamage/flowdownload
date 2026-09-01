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

    @classmethod
    def load(cls, path: str) -> "Settings":
        if not os.path.exists(path):
            return cls()
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(asdict(self), f, indent=2)
