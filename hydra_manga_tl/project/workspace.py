"""Project, import, editing, pipeline, and export orchestration."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
from typing import Callable

from PySide6.QtCore import QObject, Signal

from hydra_manga_tl.project.discovery import SUPPORTED, discover, image_path_sort_key
from hydra_manga_tl.core.ai_bridge import HYDRA_AI
from hydra_manga_tl.project.editor import RegionEdit
from hydra_manga_tl.core.language import resolve_source_language
from hydra_manga_tl.phase.job_manifest import JobManifest
from hydra_manga_tl.project.manual_region import ManualRegionService
from hydra_manga_tl.core.normalization import normalize_global_text
from hydra_manga_tl.core.paths import PATHS
from hydra_manga_tl.phase.pipeline import PipelineService
from hydra_manga_tl.project.model import MangaProject
from hydra_manga_tl.project.recent_projects import (
    LANGUAGE_NAMES,
    RECENT_PROJECT_LIMIT,
    RecentProjectSummary,
    RecentProjectsMixin,
    _derive_state_summary,
    _export_relative_time,
)
from hydra_manga_tl.project.review_queue import ReviewQueueMixin
from hydra_manga_tl.project.manual_region_workspace import ManualRegionWorkspaceMixin
from hydra_manga_tl.project.workspace_pipeline import WorkspacePipelineMixin
from hydra_manga_tl.project.workspace_editor import (
    WorkspaceEditorMixin,
    _box_text_layout,
    _polygon_box,
)
from hydra_manga_tl.phase.render_queue import RENDER_QUEUE, RenderQueue
from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.core.user_errors import render_error
from hydra_manga_tl.translation.memory import (
    TRANSLATION_MEMORY,
    source_region_hash,
)

LOGGER = logging.getLogger(__name__)
class WorkspaceManager(
    RecentProjectsMixin,
    WorkspaceEditorMixin,
    ReviewQueueMixin,
    ManualRegionWorkspaceMixin,
    WorkspacePipelineMixin,
    QObject,
):
    project_opened = Signal(object)
    project_closed = Signal()
    image_updated = Signal(int)
    pipeline_finished = Signal(bool)
    manual_region_started = Signal(int)
    manual_region_finished = Signal(int, str)
    manual_region_failed = Signal(int, str)
    manual_region_busy_changed = Signal(bool)
    translation_request_state_changed = Signal(str, str, str)
    parallel_stats_changed = Signal(object)

    def __init__(
        self,
        paths=PATHS,
        pipeline: PipelineService | None = None,
        manual_service: ManualRegionService | None = None,
        render_queue: RenderQueue | None = None,
    ) -> None:
        super().__init__()
        self.paths = paths
        self.pipeline = pipeline or PipelineService()
        self.manual_service = manual_service or ManualRegionService()
        self.render_queue = render_queue or RENDER_QUEUE
        self.current: MangaProject | None = None
        self._active_job_ids: list[str] = []
        self._active_job_completed = 0
        self._manual_render_contexts: dict[str, dict] = {}
        self.pipeline.progress.connect(self._on_progress)
        self.pipeline.image_finished.connect(self._on_image_finished)
        self.pipeline.image_failed.connect(self._on_image_failed)
        self.pipeline.completed.connect(self._on_completed)
        if hasattr(self.pipeline, "request_state_changed"):
            self.pipeline.request_state_changed.connect(self.translation_request_state_changed)
        if hasattr(self.pipeline, "scheduler_stats"):
            self.pipeline.scheduler_stats.connect(self.parallel_stats_changed)
        self.manual_service.succeeded.connect(self._on_manual_region_succeeded)
        self.manual_service.failed.connect(self._on_manual_region_failed)
        self.manual_service.busy_changed.connect(self.manual_region_busy_changed)
        if hasattr(self.manual_service, "state_changed"):
            self.manual_service.state_changed.connect(self.translation_request_state_changed)
        self.render_queue.completed.connect(self._on_render_queue_completed)
        self.render_queue.failed.connect(self._on_render_queue_failed)
        if hasattr(self.render_queue, "cancelled"):
            self.render_queue.cancelled.connect(self._on_render_queue_cancelled)
        HYDRA_AI.import_historical(self.paths.projects)

    def create_from_inputs(self, paths: list[Path], name: str | None = None) -> MangaProject:
        sources = self._resolve_inputs(paths)
        return self.create_from_sources(sources, name or self._default_project_name(paths))

    def create_from_sources(self, sources: list[tuple[Path, str]], name: str) -> MangaProject:
        """Create a project from already inspected, ordered image sources."""
        if not sources:
            raise ValueError("No supported images were found.")
        project = MangaProject.create(name, self.paths.projects)
        project.root = str((self.paths.projects / project.id).resolve())
        project.literal_provider = SETTINGS.literal_provider
        project.localization_provider = SETTINGS.localization_provider
        project.localization_model = SETTINGS.model_for(SETTINGS.localization_provider)
        project.add_sources(sources)
        project.save()
        self._set_current(project)
        return project

    @staticmethod
    def _default_project_name(paths: list[Path]) -> str:
        if len(paths) == 1:
            path = paths[0]
            return path.name if path.is_dir() else path.stem

        resolved = [path.resolve() for path in paths]
        folders = [path for path in resolved if path.is_dir()]
        if len(folders) == 1:
            folder = folders[0]
            if all(path == folder or folder in path.parents for path in resolved):
                return folder.name

        if resolved and all(path.is_file() for path in resolved):
            parents = {path.parent for path in resolved}
            if len(parents) == 1:
                return resolved[0].parent.name

        return "Manga Translation"

    def _resolve_inputs(self, inputs: list[Path]) -> list[tuple[Path, str]]:
        found: list[tuple[Path, str]] = []
        for entry in inputs:
            entry = entry.resolve()
            if entry.is_file() and entry.suffix.lower() in SUPPORTED:
                found.append((entry, entry.name))
            elif entry.is_dir():
                for image in discover(entry):
                    found.append((image.resolve(), str(image.resolve().relative_to(entry))))
        unique = {}
        for source, relative in found:
            unique.setdefault(source, relative)
        return [
            (source, relative)
            for source, relative in sorted(unique.items(), key=lambda pair: image_path_sort_key(pair[0]))
        ]

    def add_inputs(self, paths: list[Path]) -> int:
        if self.current is None:
            self.create_from_inputs(paths)
            return len(self.current.images)
        added = self.current.add_sources(self._resolve_inputs(paths))
        self.save()
        APP_STATE.refresh_project()
        return added

    def open_project(self, path: Path, *, allow_migration: bool = False) -> MangaProject:
        project_file = path / "project.json" if path.is_dir() else path
        payload = json.loads(project_file.read_text(encoding="utf-8"))
        if "documents" in payload and "images" not in payload:
            return self.import_phase2(Path(payload["documents"][0]["result_path"]).parent, legacy_payload=payload)
        project = MangaProject.load(project_file)
        self._set_current(project)
        return project

    def load_project(self, path: Path, *, allow_migration: bool = False) -> MangaProject:
        return self.open_project(path, allow_migration=allow_migration)

    def import_phase2(self, folder: Path, legacy_payload: dict | None = None) -> MangaProject:
        files = sorted(folder.glob("*_translated_*.json"))
        if not files:
            raise ValueError("No Phase 2 translation results were found.")
        first = json.loads(files[0].read_text(encoding="utf-8"))
        project = MangaProject.create(folder.parent.name or "Imported Translation", self.paths.projects)
        project.root = str((self.paths.projects / project.id).resolve())
        edits_by_result = {}
        if legacy_payload:
            edits_by_result = {Path(document["result_path"]).resolve(): document.get("edits", {}) for document in legacy_payload.get("documents", [])}
        for result_path in files:
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            source = Path(payload["source"])
            project.add_sources([(source, source.name)])
            record = project.images[-1]
            record.status = "review" if any(group.get("status") == "review" for group in payload.get("translation_groups", [])) else "ready"
            record.source_language = payload.get("source_language", "")
            record.translation_result = str(result_path.resolve())
            for key, value in edits_by_result.get(result_path.resolve(), {}).items():
                record.edits[key] = RegionEdit(**value)
            candidates = [folder.parent / "phase4" / "rendered", folder.parent / "phase3"]
            for candidate in candidates:
                rendered = candidate / f"{source.stem}_translated_en.png"
                if rendered.is_file():
                    record.rendered_image = str(rendered.resolve()); break
        project.save(); self._set_current(project); return project

    def _set_current(self, project: MangaProject) -> None:
        self._recover_interrupted_project(project)
        self.current = project
        APP_STATE.set_project(project)
        if project.images:
            APP_STATE.select(min(project.selected_image, len(project.images) - 1), 0)
        APP_STATE.set_dirty(False)
        self._remember(project.project_file)
        self.project_opened.emit(project)

    def activate_project(self, project: MangaProject) -> None:
        self._set_current(project)

    @staticmethod
    def _recover_interrupted_project(project: MangaProject) -> None:
        stale = {"preprocessing", "OCR", "ocr", "translating", "localizing", "rendering", "reconstructing", "review", "analyzing"}
        changed = False
        manifest = JobManifest.load(project.artifacts / "chapter_job_manifest.json")

        def checkpoint_exists(image_id: str, stage: str) -> bool:
            if stage == "OCR":
                return (project.artifacts / f"{image_id}_ocr.json").is_file()
            return False

        recovered = manifest.recover_stale(checkpoint_exists)
        for image in project.images:
            manifest_page = manifest.pages.get(image.id)
            if manifest_page is not None and manifest_page.state == "done":
                translation_path = project.artifacts / f"{image.id}_translated_{project.target_language}.json"
                render_dir = project.artifacts / image.id
                source_stem = Path(image.source_path).stem
                rendered_path = render_dir / f"{source_stem}_translated_en.png"
                preview_path = render_dir / f"{source_stem}_preview.png"
                if translation_path.is_file() and rendered_path.is_file():
                    try:
                        payload = json.loads(translation_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError, TypeError):
                        payload = {}
                    review = any(
                        group.get("status") == "review"
                        for group in payload.get("translation_groups", [])
                        if isinstance(group, dict)
                    ) or int(payload.get("ai_review", {}).get("issue_count", 0) or 0) > 0
                    image.status = "review" if review else "ready"
                    image.ocr_result = str(project.artifacts / f"{image.id}_ocr.json")
                    image.translation_result = str(translation_path)
                    image.rendered_image = str(rendered_path)
                    image.preview_image = str(preview_path)
                    image.error = ""
                    changed = True
                    continue
            if image.status not in stale and image.id not in recovered:
                continue
            ocr_path = project.artifacts / f"{image.id}_ocr.json"
            image.status = "partial" if ocr_path.is_file() else "queued"
            image.error = ""
            changed = True
        if changed:
            project.save()

    def save(self) -> None:
        if self.current is None:
            return
        if self.current.images:
            self.current.selected_image = max(-1, min(APP_STATE.selected_image, len(self.current.images) - 1))
        else:
            self.current.selected_image = -1
        self.current.save()
        APP_STATE.set_dirty(False)

    def close(self) -> None:
        if self.pipeline.running:
            self.pipeline.cancel()
        self.save()
        self.current = None
        APP_STATE.reset()
        self.project_closed.emit()

    @property
    def active_job_ids(self) -> tuple[str, ...]:
        return tuple(self._active_job_ids)

    def reorder_images(self, ordered_ids: list[str]) -> bool:
        """Persist a complete page order while preserving the selected image."""
        if self.current is None or APP_STATE.busy or self.pipeline.running:
            return False
        existing_ids = [image.id for image in self.current.images]
        if len(ordered_ids) != len(existing_ids) or len(set(ordered_ids)) != len(ordered_ids):
            return False
        if set(ordered_ids) != set(existing_ids):
            return False
        if ordered_ids == existing_ids:
            return True

        selected_id = None
        if 0 <= APP_STATE.selected_image < len(self.current.images):
            selected_id = self.current.images[APP_STATE.selected_image].id
        by_id = {image.id: image for image in self.current.images}
        self.current.images = [by_id[image_id] for image_id in ordered_ids]
        selected_index = ordered_ids.index(selected_id) if selected_id in by_id else 0
        self.current.selected_image = selected_index
        self.current.save()
        APP_STATE.set_dirty(False)
        APP_STATE.select(selected_index, APP_STATE.selected_block)
        APP_STATE.refresh_project()
        return True

    def remove_images(self, image_ids: set[str]) -> int:
        """Remove selected pages from the project without deleting source files."""
        if self.current is None or APP_STATE.busy or self.pipeline.running:
            return 0
        requested = {str(image_id) for image_id in image_ids if str(image_id)}
        existing = list(self.current.images)
        removed = [image for image in existing if image.id in requested]
        if not removed:
            return 0
        if len(removed) >= len(existing):
            raise ValueError("A project must keep at least one image.")

        selected_index = max(0, min(APP_STATE.selected_image, len(existing) - 1))
        selected_id = existing[selected_index].id
        remaining = [image for image in existing if image.id not in requested]
        if selected_id in {image.id for image in remaining}:
            next_id = selected_id
        else:
            next_record = next(
                (image for image in existing[selected_index + 1:] if image.id not in requested),
                None,
            )
            if next_record is None:
                next_record = next(
                    image for image in reversed(existing[:selected_index])
                    if image.id not in requested
                )
            next_id = next_record.id

        next_index = next(
            index for index, image in enumerate(remaining) if image.id == next_id
        )
        removed_sources = {
            str(Path(image.source_path).resolve())
            for image in removed
        }
        if self.current.recent_thumbnail:
            try:
                current_thumbnail = str(Path(self.current.recent_thumbnail).resolve())
            except (OSError, RuntimeError):
                current_thumbnail = self.current.recent_thumbnail
            if current_thumbnail in removed_sources:
                self.current.recent_thumbnail = ""
        self.current.images = remaining
        self.current.selected_image = next_index
        self.current.save()
        APP_STATE.set_dirty(False)
        APP_STATE.select(next_index, -1)
        APP_STATE.refresh_project()
        return len(removed)

    def set_recent_thumbnail(self, image_id: str) -> Path:
        """Use a project image as the landing/recent-project thumbnail."""
        if self.current is None:
            raise ValueError("No project is open.")
        image = next((item for item in self.current.images if item.id == image_id), None)
        if image is None:
            raise ValueError("The selected page is not in the current project.")
        thumbnail = Path(image.source_path)
        if not thumbnail.is_file():
            raise ValueError("The selected page image is missing from disk.")
        return self.set_recent_thumbnail_path(thumbnail)

    def set_recent_thumbnail_path(self, thumbnail_path: Path) -> Path:
        """Use an explicit image path as the landing/recent-project thumbnail."""
        if self.current is None:
            raise ValueError("No project is open.")
        thumbnail = Path(thumbnail_path)
        if not thumbnail.is_file():
            raise ValueError("The selected thumbnail image is missing from disk.")
        self.current.recent_thumbnail = str(thumbnail.resolve())
        self.current.save()
        APP_STATE.set_dirty(False)
        APP_STATE.refresh_project()
        return thumbnail

    @staticmethod
    def _file_hash(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _capture_crop(self, image_index: int, group: dict, subject: str) -> str:
        if self.current is None:
            return ""
        from PIL import Image
        image = self.current.images[image_index]
        polygon = group.get("polygon", [])
        if not polygon:
            return ""
        xs = [int(point[0]) for point in polygon]; ys = [int(point[1]) for point in polygon]
        with Image.open(image.source_path) as opened:
            x1 = max(0, min(xs)); y1 = max(0, min(ys)); x2 = min(opened.width, max(xs)); y2 = min(opened.height, max(ys))
            if x2 <= x1 or y2 <= y1:
                return ""
            folder = self.current.artifacts / "ai_capture"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{subject}.png"
            opened.crop((x1, y1, x2, y2)).save(path)
        return str(path)

    def _capture_edit_corrections(self, image_index: int, group: dict, edit: RegionEdit) -> None:
        if self.current is None:
            return
        image = self.current.images[image_index]
        subject = self.ai_subject_id(image_index, group)
        crop = self._capture_crop(image_index, group, subject)
        common = {
            "subject_id": subject, "project_id": self.current.id, "image_id": image.id,
            "profile": self.current.text_style, "source_language": "ja", "target_language": self.current.target_language,
            "confidence": float(group.get("ocr_confidence", 0.0)), "page_hash": self._file_hash(Path(image.source_path)),
            "input_path": crop, "metadata": {"group_index": str(group.get("index")), "direction": group.get("direction", ""), "bubble_type": group.get("bubble_type", "speech")},
        }
        new_bubble_type = edit.bubble_type or group.get("bubble_type", "speech")
        if new_bubble_type != group.get("bubble_type", "speech"):
            HYDRA_AI.capture_correction(event_type="region_type_corrected", task="bubble", before={"polygon": group.get("polygon"), "type": group.get("bubble_type", "speech")}, after={"polygon": group.get("polygon"), "type": new_bubble_type}, **common)
        new_original = edit.original_text if edit.original_text is not None else group.get("original_text", "")
        if new_original != group.get("original_text", ""):
            HYDRA_AI.capture_correction(event_type="ocr_text_corrected", task="ocr", before={"text": group.get("original_text", "")}, after={"text": new_original}, **common)
        if edit.translated_text is not None and edit.translated_text != group.get("translated_text", ""):
            HYDRA_AI.capture_correction(event_type="translation_corrected", task="translation", before={"source": new_original, "translated_text": group.get("translated_text", "")}, after={"source": new_original, "translated_text": edit.translated_text}, **common)
        before_layout = {
            "font_size": int(group.get("font_size_override", 0) or 0), "offset_x": int((group.get("placement_offset") or [0, 0])[0]),
            "offset_y": int((group.get("placement_offset") or [0, 0])[1]), "font_family": group.get("font_family", "Arial"),
            "alignment": group.get("alignment", "center"), "text": group.get("translated_text", ""),
        }
        after_layout = {"font_size": edit.font_size, "offset_x": edit.offset_x, "offset_y": edit.offset_y, "font_family": edit.font_family, "alignment": edit.alignment, "text": edit.translated_text or group.get("translated_text", "")}
        if before_layout != after_layout:
            HYDRA_AI.capture_correction(event_type="layout_corrected", task="layout", before=before_layout, after=after_layout, **common)

    def _learn_user_translation_edit(
        self,
        image_index: int,
        group: dict,
        edit: RegionEdit,
    ) -> None:
        if (
            self.current is None
            or not SETTINGS.translation_memory_enabled
            or not SETTINGS.translation_memory_store_user_edits
            or edit.translated_text is None
            or edit.translated_text == group.get("translated_text", "")
        ):
            return
        image = self.current.images[image_index]
        source = (
            edit.original_text
            if edit.original_text is not None
            else str(group.get("original_text", ""))
        )
        polygons = (
            group.get("source_polygons")
            or ([group.get("polygon")] if group.get("polygon") else [])
        )
        TRANSLATION_MEMORY.record_user_edit(
            source_text=str(source),
            translated_text=normalize_global_text(edit.translated_text),
            source_language=resolve_source_language(
                self.current.source_language,
                image.source_language,
                group.get("source_language"),
            ),
            target_language=self.current.target_language,
            region_type=normalize_region_type(
                edit.bubble_type or group.get("bubble_type")
            ),
            source_region_hash=source_region_hash(
                image.source_path,
                polygons,
            ),
            translation_provider="user",
            quality_score=1.0,
            project_id=self.current.id,
        )

    def approve_ai_block(self, image_index: int, group_index: int | str):
        if self.current is None:
            return None
        group = next((item for item in self.effective_translation_payload(image_index)["translation_groups"] if str(item["index"]) == str(group_index)), None)
        if group is None:
            return None
        subject = self.ai_subject_id(image_index, group)
        self._capture_review_outcome(image_index, group, subject)
        summary = HYDRA_AI.approve([subject])
        if summary is not None:
            self._mark_ai_subjects_approved(image_index, [subject])
        return summary

    def approve_ai_page(self, image_index: int):
        if self.current is None:
            return None
        groups = self.effective_translation_payload(image_index)["translation_groups"]
        return self._approve_ai_groups(image_index, groups)

    def approve_ai_page_bubbles(self, image_index: int):
        if self.current is None:
            return None
        groups = [
            group for group in self.effective_translation_payload(image_index)["translation_groups"]
            if self.ocr_review_reasons(group)
        ]
        return self._approve_ai_groups(image_index, groups)

    def approve_ai_page_reviews(self, image_index: int):
        if self.current is None:
            return None
        groups = []
        for group in self.effective_translation_payload(image_index)["translation_groups"]:
            ocr_reasons = set(self.ocr_review_reasons(group))
            review_reasons = [
                str(reason) for reason in group.get("review_reasons", [])
                if str(reason) and str(reason) not in ocr_reasons
            ]
            if review_reasons or (group.get("status") == "review" and not ocr_reasons):
                groups.append(group)
        return self._approve_ai_groups(image_index, groups)

    def _approve_ai_groups(self, image_index: int, groups: list[dict]):
        subjects = []
        for group in groups:
            subject = self.ai_subject_id(image_index, group)
            self._capture_review_outcome(image_index, group, subject)
            subjects.append(subject)
        summary = HYDRA_AI.approve(subjects)
        if summary is not None:
            self._mark_ai_subjects_approved(image_index, subjects)
        return summary

    def _capture_review_outcome(self, image_index: int, group: dict, subject: str) -> None:
        if self.current is None:
            return
        image = self.current.images[image_index]
        corrected = str(group.get("index")) in image.edits or bool(group.get("manual"))
        HYDRA_AI.capture_correction(
            event_type="review_outcome", task="quality", subject_id=subject,
            project_id=self.current.id, image_id=image.id,
            before={"prediction": {"ocr_confidence": group.get("ocr_confidence", 0.0), "status": group.get("status", "")}},
            after={"corrected": corrected, "approved_unchanged": not corrected},
            profile=self.current.text_style, source_language="ja", target_language=self.current.target_language,
            confidence=float(group.get("ocr_confidence", 0.0)), page_hash=self._file_hash(Path(image.source_path)),
            input_path=self._capture_crop(image_index, group, subject),
            metadata={"group_index": str(group.get("index")), "review_reasons": group.get("review_reasons", [])},
        )

    def shutdown(self) -> None:
        self.cancel_active_requests()

    def rerender_image(self, image_index: int, log_callback: Callable[[str], None] | None = None) -> Path:
        if self.current is None:
            raise ValueError("No project is open.")
        image = self.current.images[image_index]
        payload = self.effective_translation_payload(image_index)
        working = self.current.artifacts / "editor"
        working.mkdir(parents=True, exist_ok=True)
        result_path = working / f"{image.id}_translated_en.json"
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        render_dir = self.current.artifacts / image.id
        for line in self._run_editor_render(result_path, render_dir):
            if log_callback is not None:
                log_callback(line)
        source = Path(image.source_path)
        image.rendered_image = str(render_dir / f"{source.stem}_translated_en.png")
        image.preview_image = str(render_dir / f"{source.stem}_preview.png")
        self._persist_rendered_style_profiles(image_index, render_dir)
        self.save()
        self.image_updated.emit(image_index)
        return Path(image.rendered_image)

    def _persist_rendered_style_profiles(self, image_index: int, render_dir: Path) -> None:
        if self.current is None:
            return
        image = self.current.images[image_index]
        report_path = render_dir / f"{Path(image.source_path).stem}_render.json"
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return
        for rendered in report.get("rendered_groups", []):
            style_profile = rendered.get("style_profile")
            if not isinstance(style_profile, dict):
                continue
            group_key = str(rendered.get("group", ""))
            if not group_key:
                continue
            previous = image.edits.get(group_key)
            edit = RegionEdit(**asdict(previous)) if previous is not None else RegionEdit()
            edit.style_profile = style_profile
            image.edits[group_key] = edit

    @staticmethod
    def _editor_render_command(result_path: Path, render_dir: Path, policy: str) -> list[str]:
        if getattr(sys, "frozen", False):
            return [
                sys.executable,
                "--phase3-render",
                str(result_path),
                "--output",
                str(render_dir),
                "--policy",
                policy,
            ]
        return [
            sys.executable,
            "-m",
            "hydra_manga_tl.phase.phase3",
            str(result_path),
            "--output",
            str(render_dir),
            "--policy",
            policy,
        ]

    @staticmethod
    def _process_editor_render_events() -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        from PySide6.QtWidgets import QApplication

        application = QApplication.instance()
        if application is not None:
            application.processEvents()

    @classmethod
    def _run_editor_render(cls, result_path: Path, render_dir: Path, policy: str = "complete") -> list[str]:
        creation_flags = (
            subprocess.CREATE_NO_WINDOW
            if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW")
            else 0
        )
        repo_root = Path(__file__).resolve().parents[2]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        python_path = env.get("PYTHONPATH", "")
        repo_root_text = str(repo_root)
        env["PYTHONPATH"] = (
            repo_root_text
            if not python_path
            else os.pathsep.join([repo_root_text, python_path])
        )
        process = subprocess.Popen(
            cls._editor_render_command(result_path, render_dir, policy),
            cwd=repo_root_text,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            creationflags=creation_flags,
        )
        while True:
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                cls._process_editor_render_events()
        if process.returncode != 0:
            details = (stderr or stdout or "").strip().splitlines()
            message = details[-1] if details else f"renderer exited with code {process.returncode}"
            raise RuntimeError(message)
        return [
            line.strip()
            for line in (stdout or stderr or "").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _render_failure_message(error: BaseException) -> str:
        return render_error(error)


WORKSPACE = WorkspaceManager()
