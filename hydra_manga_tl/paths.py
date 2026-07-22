"""Application-owned paths."""

from __future__ import annotations

import os
from pathlib import Path


class AppPaths:
    def __init__(self, root: Path | None = None) -> None:
        base = root or Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Hydra Manga TL"
        self.root = base.resolve()
        self.projects = self.root / "projects"
        self.settings = self.root / "settings.json"
        self.recent = self.root / "recent.json"
        self.logs = self.root / "logs"

    def initialize(self) -> None:
        self.projects.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)


PATHS = AppPaths()
