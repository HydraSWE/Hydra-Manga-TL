"""Optional bridge to the private HydraMangaAi package.

The public application must remain fully functional when the private package is
not present, not initialized, or has no active model.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .settings import SETTINGS


class OptionalHydraAi:
    def __init__(self) -> None:
        self._engine = None
        self._draft_type = None
        self.error = ""

    def _load(self):
        if not SETTINGS.ai_enabled:
            return None
        if self._engine is not None:
            return self._engine
        try:
            from HydraMangaAi import CorrectionDraft, HydraAiEngine
            self._draft_type = CorrectionDraft
            self._engine = HydraAiEngine(SETTINGS.ai_data_root)
        except Exception as error:  # optional boundary must never stop the app
            self.error = f"{type(error).__name__}: {error}"
            self._engine = None
        return self._engine

    @property
    def available(self) -> bool:
        return self._load() is not None

    def capture_correction(self, *, input_path: str = "", target_path: str = "", **values) -> str:
        engine = self._load()
        if engine is None or self._draft_type is None:
            return ""
        try:
            input_blob = engine.store.put_blob(Path(input_path), "image/png") if input_path and Path(input_path).is_file() else ""
            target_blob = engine.store.put_blob(Path(target_path), "image/png") if target_path and Path(target_path).is_file() else ""
            draft = self._draft_type(**values, input_blob=input_blob, target_blob=target_blob)
            return engine.capture_correction(draft)
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            return ""

    def approve(self, subject_ids):
        engine = self._load()
        if not engine:
            return None
        try:
            return engine.approve(subject_ids)
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            return None

    def invalidate(self, sample_ids, purge_blobs: bool = False):
        engine = self._load()
        return engine.invalidate(sample_ids, purge_blobs) if engine else None

    def predict(self, task: str, value: dict, profile: str = "Manga"):
        engine = self._load()
        return engine.predict(task, value, profile) if engine else None

    def queue_training(self, task: str, profile: str = "Manga") -> str:
        engine = self._load()
        return engine.queue_training(task, profile) if engine else ""

    def training_dry_run(self, task: str, profile: str = "Manga") -> dict[str, Any]:
        engine = self._load()
        if engine:
            return engine.training_dry_run(task, profile)
        return {"task": task, "profile": profile, "ready": False, "error": self.error or "Private HydraMangaAi package is unavailable."}

    def pause_training(self) -> None:
        engine = self._load()
        if engine: engine.pause_training()

    def resume_training(self) -> None:
        engine = self._load()
        if engine: engine.resume_training()

    def model_status(self) -> dict[str, Any]:
        engine = self._load()
        if engine:
            return engine.model_status()
        return {"counts": {}, "active": {}, "paused": False, "eligibility": {}, "error": self.error or "Private HydraMangaAi package is unavailable."}

    def rollback(self, task: str, profile: str, version: str) -> bool:
        engine = self._load()
        return bool(engine and engine.rollback(task, profile, version))

    def import_historical(self, projects_root: Path) -> dict[str, int]:
        engine = self._load()
        if not engine:
            return {"projects": 0, "pages": 0, "artifacts": 0}
        try:
            return engine.import_historical(projects_root)
        except Exception as error:
            self.error = f"{type(error).__name__}: {error}"
            return {"projects": 0, "pages": 0, "artifacts": 0}


HYDRA_AI = OptionalHydraAi()
