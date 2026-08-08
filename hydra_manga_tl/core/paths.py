"""Application-owned paths."""

from __future__ import annotations

import os
from pathlib import Path
import shutil


VALID_PROFILES = {"stable", "development"}
CURRENT_CACHE_SCHEMA = 11


def default_profile() -> str:
    requested = os.environ.get("HYDRA_PROFILE", "stable").strip().casefold()
    return requested if requested in VALID_PROFILES else "stable"


def default_app_data_root() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local")) / "Hydra Manga TL"


class AppPaths:
    def __init__(self, root: Path | None = None, profile: str | None = None) -> None:
        self.profile = self._normalize_profile(profile or default_profile())
        self.configure(root, profile=self.profile)

    @staticmethod
    def _normalize_profile(profile: str) -> str:
        normalized = str(profile).strip().casefold()
        if normalized not in VALID_PROFILES:
            raise ValueError(
                f"Unknown Hydra profile {profile!r}. Expected stable or development."
            )
        return normalized

    @staticmethod
    def default_root() -> Path:
        return default_app_data_root()

    def configure(
        self,
        root: Path | str | None = None,
        *,
        profile: str | None = None,
    ) -> None:
        base = Path(root).expanduser() if root else default_app_data_root()
        if profile is not None:
            self.profile = self._normalize_profile(profile)
        self.root = base.resolve()
        self.projects = self.root / "projects"
        self.shared = self.root / "shared"
        self.profiles = self.root / "profiles"
        self.profile_root = self.profiles / self.profile
        self.profile_settings = self.profile_root / "settings.json"
        self.recent = self.profile_root / "recent.json"
        self.window_state = self.profile_root / "window.json"
        self.ui_state = self.profile_root / "ui_state.json"
        # Legacy aliases remain readable so existing installations can migrate
        # without moving or deleting user-owned state.
        self.settings = self.root / "settings.json"
        self.legacy_recent = self.root / "recent.json"
        self.logs = self.root / "logs"
        self.cache = self.root / "cache"
        self.schema_cache = self.cache_for_schema(CURRENT_CACHE_SCHEMA)
        self.ocr_cache = self.schema_cache / "ocr"
        self.page_translation_cache = self.schema_cache / "page_translation"
        legacy_tm = self.root / "translation_memory.db"
        legacy_phrase = self.root / "phrase_memory.db"
        self.translation_memory = (
            legacy_tm if legacy_tm.is_file()
            else self.shared / "translation_memory.db"
        )
        self.phrase_memory = (
            legacy_phrase if legacy_phrase.is_file()
            else self.shared / "phrase_memory.db"
        )
        self.legacy_translation_memory = self.root / "translation_memory.json"
        self.title_style_cache = self.root / "title_style_cache"

    def initialize(self) -> None:
        self.projects.mkdir(parents=True, exist_ok=True)
        self.shared.mkdir(parents=True, exist_ok=True)
        self.profile_root.mkdir(parents=True, exist_ok=True)
        self.logs.mkdir(parents=True, exist_ok=True)
        self.ocr_cache.mkdir(parents=True, exist_ok=True)
        self.page_translation_cache.mkdir(parents=True, exist_ok=True)
        self.title_style_cache.mkdir(parents=True, exist_ok=True)
        self._copy_legacy_profile_state()

    def _copy_legacy_profile_state(self) -> None:
        """Seed a profile once; never delete or overwrite legacy state."""
        if self.profile != "stable":
            return
        for legacy, profiled in (
            (self.settings, self.profile_settings),
            (self.legacy_recent, self.recent),
        ):
            if profiled.exists() or not legacy.is_file():
                continue
            profiled.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, profiled)

    def cache_for_schema(self, schema: int) -> Path:
        value = int(schema)
        if value < 1:
            raise ValueError("Cache schema must be a positive integer.")
        return self.cache / f"schema{value}"

    def cleanup_schema_cache(self, schema: int) -> Path | None:
        """Remove only the requested schema cache, never shared app data."""
        target = self.cache_for_schema(schema).resolve()
        cache_root = self.cache.resolve()
        if target.parent != cache_root:
            raise ValueError("Schema cache target escaped the cache root.")
        if not target.exists():
            return None
        shutil.rmtree(target)
        return target


PATHS = AppPaths()
