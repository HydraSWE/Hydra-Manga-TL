"""Application-owned paths."""

from __future__ import annotations

import os
from pathlib import Path


def default_app_data_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Hydra Manga TL"


class AppPaths:
    def __init__(self, root: Path | None = None) -> None:
        self.configure(root)

    @staticmethod
    def default_root() -> Path:
        return default_app_data_root()

    def configure(self, root: Path | str | None = None) -> None:
        base = Path(root).expanduser() if root else default_app_data_root()
        self.root = base.resolve()
        self.projects = self.root / "projects"
        self.settings = self.root / "settings.json"
        self.recent = self.root / "recent.json"
        self.logs = self.root / "logs"
        self.cache = self.root / "cache"
        self.ocr_cache = self.cache / "ocr"
        self.page_translation_cache = self.cache / "page_translation"
        self.translation_memory = self.root / "translation_memory.db"
        self.phrase_memory = self.root / "phrase_memory.db"
        self.legacy_translation_memory = self.root / "translation_memory.json"
        self.title_style_cache = self.root / "title_style_cache"

    def initialize(self) -> None:
        self.projects.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.ocr_cache.mkdir(parents=True, exist_ok=True)
        self.page_translation_cache.mkdir(parents=True, exist_ok=True)
        self.title_style_cache.mkdir(parents=True, exist_ok=True)


PATHS = AppPaths()
