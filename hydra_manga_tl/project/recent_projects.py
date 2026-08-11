"""Recent-project persistence, summaries, and guarded data cleanup."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from hydra_manga_tl import __version__


RECENT_PROJECT_LIMIT = 500


@dataclass(frozen=True)
class RecentProjectSummary:
    path: Path
    name: str
    source_language: str
    target_language: str
    page_count: int
    last_opened: str
    thumbnail_path: Path | None = None
    schema: str = "v1"
    created_by: str = ""
    last_saved_by: str = ""
    minimum_app_version: str = __version__
    compatibility_status: str = "compatible"
    compatibility_message: str = ""
    # Processing state derived from persisted ImageRecord.status values
    state_label: str = ""
    state_ready: int = 0
    state_review: int = 0
    state_failed: int = 0
    state_queued: int = 0
    state_display: str = ""
    # Last successful export metadata (empty when never exported)
    exported: bool = False
    export_relative_time: str = ""
    export_type: str = ""
    export_count: int = 0
    export_destination: str = ""
    export_format: str = ""

LANGUAGE_NAMES = {
    "en": "English",
    "ja": "Japanese",
    "japan": "Japanese",
    "ko": "Korean",
    "korean": "Korean",
    "zh": "Chinese",
    "ch": "Chinese",
}


_STATE_REVIEW = {"review"}
_STATE_READY = {"done", "ready"}
_STATE_FAILED = {"failed", "cancelled"}
_STATE_QUEUED = {
    "queued", "pending", "partial", "preprocessing", "analyzing",
    "OCR", "ocr", "translating", "localizing", "rendering", "reconstructing",
}


def _derive_state_summary(images: list) -> dict:
    """Derive processing state counts from raw image list read from project.json.

    Returns a dict of kwargs suitable for RecentProjectSummary state fields.
    No model deserialization is performed — only raw dicts are accepted.
    """
    ready = review = failed = queued = 0
    for img in images:
        if not isinstance(img, dict):
            continue
        status = str(img.get("status", "queued") or "queued")
        if status in _STATE_READY:
            ready += 1
        elif status in _STATE_REVIEW:
            review += 1
        elif status in _STATE_FAILED:
            failed += 1
        else:
            queued += 1
    total = ready + review + failed + queued
    if total == 0:
        label, display = "Not Started", "Not Started"
    elif failed > 0:
        parts = [f"{failed} failed"]
        if ready:
            parts.append(f"{ready} ready")
        label = "Failed"
        display = "Failed \u2022 " + " \u2022 ".join(parts)
    elif review > 0:
        parts = [f"{review} review"]
        if ready:
            parts.append(f"{ready} ready")
        label = "Needs Review"
        display = "Needs Review \u2022 " + " \u2022 ".join(parts)
    elif queued > 0 and ready > 0:
        label = "Partial"
        display = f"Partial \u2022 {ready} ready \u2022 {queued} pending"
    elif queued > 0:
        label, display = "Not Started", "Not Started"
    else:
        label = "Ready"
        display = f"Ready \u2022 {ready} pages"
    return {
        "state_label": label,
        "state_ready": ready,
        "state_review": review,
        "state_failed": failed,
        "state_queued": queued,
        "state_display": display,
    }


def _export_relative_time(iso_str: str) -> str:
    """Convert an ISO-8601 UTC string to a compact human-readable relative display."""
    if not iso_str:
        return ""
    try:
        dt = datetime.fromisoformat(iso_str)
        now = datetime.now(timezone.utc)
        delta = now - dt.astimezone(timezone.utc)
        days = delta.days
        if days < 0:
            days = 0
        if days == 0:
            return "Today"
        if days == 1:
            return "Yesterday"
        if days < 30:
            return f"{days} days ago"
        # Cross-platform month + day (avoids %-d / %#d platform differences)
        return dt.strftime("%b").strip() + " " + str(dt.day)
    except (ValueError, OverflowError, OSError):
        return ""




class RecentProjectsMixin:
    """Provide recent-project behavior to WorkspaceManager."""

    def _recent_entries(self) -> list[tuple[Path, str]]:
        if not self.paths.recent.is_file():
            return []
        try:
            payload = json.loads(self.paths.recent.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(payload, list):
            return []
        entries: list[tuple[Path, str]] = []
        for value in payload:
            if isinstance(value, str):
                path, last_opened = Path(value), ""
            elif isinstance(value, dict) and isinstance(value.get("path"), str):
                path = Path(value["path"])
                last_opened = value.get("last_opened", "")
                if not isinstance(last_opened, str):
                    last_opened = ""
            else:
                continue
            if path.is_file():
                entries.append((path, last_opened))
        return entries[:RECENT_PROJECT_LIMIT]

    def recent_projects(self) -> list[Path]:
        return [path for path, _ in self._recent_entries()]

    def project_metadata(self, path: Path):
        from hydra_manga_tl.project.compatibility import inspect_project
        from hydra_manga_tl.project.model import PROJECT_VERSION
        return inspect_project(path, current_schema=PROJECT_VERSION)

    def recent_project_summaries(self) -> list[RecentProjectSummary]:
        summaries: list[RecentProjectSummary] = []
        for path, last_opened in self._recent_entries():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if not isinstance(payload, dict):
                continue
            name = payload.get("name")
            name = name.strip() if isinstance(name, str) and name.strip() else "Unnamed project"
            images = payload.get("images", [])
            images = images if isinstance(images, list) else []
            languages = [
                image.get("source_language", "").strip()
                for image in images
                if isinstance(image, dict) and isinstance(image.get("source_language"), str)
                and image.get("source_language", "").strip()
            ]
            source = Counter(languages).most_common(1)[0][0] if languages else "Auto-detect"
            source = LANGUAGE_NAMES.get(source.casefold(), source)
            target = payload.get("target_language", "en")
            target = target.strip() if isinstance(target, str) and target.strip() else "en"
            target = LANGUAGE_NAMES.get(target.casefold(), target.upper())
            thumbnail_path = None
            thumbnail_raw = payload.get("recent_thumbnail")
            if isinstance(thumbnail_raw, str) and thumbnail_raw.strip():
                candidate = Path(thumbnail_raw)
                if candidate.is_file():
                    thumbnail_path = candidate
            summaries.append(
                RecentProjectSummary(
                    path=path,
                    name=name,
                    source_language=source,
                    target_language=target,
                    page_count=len(images),
                    last_opened=last_opened,
                    thumbnail_path=thumbnail_path,
                    **_derive_state_summary(images),
                    exported=bool(payload.get("last_exported_at")),
                    export_relative_time=_export_relative_time(
                        str(payload.get("last_exported_at") or "")
                    ),
                    export_type=str(payload.get("last_export_type") or ""),
                    export_count=int(payload.get("last_export_count") or 0),
                    export_destination=(
                        Path(str(payload.get("last_export_path") or "")).name
                        if payload.get("last_export_path")
                        else ""
                    ),
                    export_format=str(payload.get("last_export_format") or ""),
                )
            )
        return summaries

    def forget_recent_project(self, project_file: Path) -> None:
        remaining = [
            {"path": str(path), "last_opened": last_opened}
            for path, last_opened in self._recent_entries()
            if path.resolve() != project_file.resolve()
        ]
        self.paths.recent.parent.mkdir(parents=True, exist_ok=True)
        self.paths.recent.write_text(json.dumps(remaining, indent=2), encoding="utf-8")

    def recent_project_data_root(self, project_file: Path) -> Path | None:
        try:
            projects_root = self.paths.projects.resolve()
            project_root = Path(project_file).resolve().parent
        except (OSError, RuntimeError):
            return None
        if project_root == projects_root:
            return None
        if not project_root.is_relative_to(projects_root):
            return None
        if not (project_root / "project.json").is_file():
            return None
        return project_root

    def delete_recent_project_data(self, project_file: Path) -> Path | None:
        project_root = self.recent_project_data_root(project_file)
        if project_root is None or not project_root.exists():
            return None
        shutil.rmtree(project_root)
        return project_root

    def clear_recent_projects(self) -> None:
        try:
            self.paths.recent.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def project_display_name(path: Path) -> str:
        project_file = path / "project.json" if path.is_dir() else path
        try:
            payload = json.loads(project_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return "Unnamed project"
        if not isinstance(payload, dict):
            return "Unnamed project"
        name = payload.get("name")
        return name.strip() if isinstance(name, str) and name.strip() else "Unnamed project"

    def _remember(self, project_file: Path) -> None:
        remembered = [
            {"path": str(project_file), "last_opened": datetime.now(timezone.utc).isoformat()}
        ]
        remembered.extend(
            {"path": str(path), "last_opened": last_opened}
            for path, last_opened in self._recent_entries()
            if path.resolve() != project_file.resolve()
        )
        self.paths.recent.parent.mkdir(parents=True, exist_ok=True)
        self.paths.recent.write_text(
            json.dumps(remembered[:RECENT_PROJECT_LIMIT], indent=2),
            encoding="utf-8",
        )

