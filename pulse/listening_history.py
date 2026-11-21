"""Small local persistence layer for recently played tracks."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


class ListeningHistory:
    """Store a bounded list of playback sessions in the user's local config."""

    MAX_ENTRIES = 500

    def __init__(self, path: Path | None = None):
        self.path = path or (Path.home() / ".pulse-cli" / "listening_history.json")
        self.entries: List[Dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                self.entries = [entry for entry in data if isinstance(entry, dict)][-self.MAX_ENTRIES:]
        except (OSError, ValueError):
            self.entries = []

    def add(self, track) -> bool:
        """Add a playback session; return False if it could not be persisted."""
        entry = {
            "path": str(track.path),
            "title": track.title,
            "artist": track.artist,
            "album": track.album,
            "played_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.entries.insert(0, entry)
        del self.entries[self.MAX_ENTRIES:]
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(".tmp")
            temp_path.write_text(json.dumps(self.entries, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp_path, self.path)
            return True
        except OSError:
            self.entries.pop(0)
            return False

    def clear(self) -> bool:
        previous = self.entries
        self.entries = []
        try:
            self.path.unlink(missing_ok=True)
            return True
        except OSError:
            self.entries = previous
            return False
