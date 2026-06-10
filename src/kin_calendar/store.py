"""Single-user persistent state, kept as plain files in DATA_DIR.

On Railway, mount a volume at /data so this survives redeploys. Secrets the
user enters in the web UI (Kindroid key) live here too — acceptable for a
self-hosted single-user instance, and overridable via environment variables.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Optional

_DEFAULT_STATE = {
    "calendar_id": None,
    "calendar_label": None,
    "timezone": None,
    "kin_name": None,
    "kindroid_api_key": None,
    "kindroid_ai_id": None,
    "scanner_enabled": False,
    "scanner_cursor": None,
    "scanner_log": [],  # newest first, capped
    "auto_refresh_enabled": True,
    "last_week_start": None,  # set on each write; drives the weekly auto-refresh
}

_LOG_CAP = 50


class Store:
    def __init__(self, data_dir: Optional[str] = None):
        self.data_dir = Path(data_dir or os.environ.get("DATA_DIR", "./data"))
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    # -- paths ---------------------------------------------------------------

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def spine_path(self) -> Path:
        return self.data_dir / "spine.json"

    @property
    def events_path(self) -> Path:
        return self.data_dir / "events.json"

    @property
    def token_path(self) -> Path:
        return self.data_dir / "token.json"

    @property
    def backstory_path(self) -> Path:
        return self.data_dir / "backstory.txt"

    @property
    def suggestions_path(self) -> Path:
        return self.data_dir / "suggestions.json"

    # -- state ---------------------------------------------------------------

    def state(self) -> dict:
        if self.state_path.exists():
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        else:
            data = {}
        merged = dict(_DEFAULT_STATE)
        merged.update(data)
        return merged

    def update_state(self, **changes) -> dict:
        with self._lock:
            data = self.state()
            data.update(changes)
            self.state_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
            return data

    def append_log(self, entry: str) -> None:
        with self._lock:
            data = self.state()
            log = [entry] + data.get("scanner_log", [])
            data["scanner_log"] = log[:_LOG_CAP]
            self.state_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    # -- settings with env override ------------------------------------------

    def setting(self, key: str, env_var: str) -> Optional[str]:
        return os.environ.get(env_var) or self.state().get(key)

    def timezone(self) -> str:
        return self.setting("timezone", "KIN_TIMEZONE") or "UTC"

    # -- working files ---------------------------------------------------------

    def read_text(self, path: Path) -> Optional[str]:
        return path.read_text(encoding="utf-8") if path.exists() else None

    def write_text(self, path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
