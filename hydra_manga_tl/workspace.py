"""Project, import, editing, pipeline, and export orchestration."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Callable
from uuid import uuid4

from PySide6.QtCore import QObject, Signal, Slot

from .cli import SUPPORTED, discover, image_path_sort_key
from .ai_bridge import HYDRA_AI
from .editor_project import RegionEdit
from .export_system import export_archive, export_images
from .language import resolve_source_language
from .job_manifest import JobManifest
from .manual_region import ManualRegionService, overlapping_auto_indices
from .paths import PATHS
from .pipeline import PipelineService
from .project import MangaProject, ManualRegion
from .render_queue import RENDER_QUEUE, RenderQueue
from .settings import SETTINGS
from .state import APP_STATE
from .translation_requests import RenderRequest


@dataclass(frozen=True)
class RecentProjectSummary:
    path: Path
    name: str
    source_language: str
    target_language: str
    page_count: int
    last_opened: str


LANGUAGE_NAMES = {
    "en": "English",
    "ja": "Japanese",
    "japan": "Japanese",
    "ko": "Korean",
    "korean": "Korean",
    "zh": "Chinese",
    "ch": "Chinese",
}


class WorkspaceManager(QObject):
    project_opened = Signal(object)
    project_closed = Signal()
    image_updated = Signal(int)
    pipeline_finished = Signal(bool)
    manual_region_started = Signal(int)
    manual_region_finished = Signal(int, str)
    manual_region_failed = Signal(int, str)
    manual_region_busy_changed = Signal(bool)
    translation_request_state_changed = Signal(str, str, str)

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

    def open_project(self, path: Path) -> MangaProject:
        project_file = path / "project.json" if path.is_dir() else path
        payload = json.loads(project_file.read_text(encoding="utf-8"))
        if "documents" in payload and "images" not in payload:
            return self.import_phase2(Path(payload["documents"][0]["result_path"]).parent, legacy_payload=payload)
        project = MangaProject.load(project_file)
        self._set_current(project)
        return project

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
        self.current.selected_image = max(0, APP_STATE.selected_image)
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

    def start_pipeline(self, image_ids: set[str] | None = None, *, retranslate: bool = False) -> bool:
        if self.current is None:
            return False
        eligible = {"pending", "queued", "partial", "failed", "cancelled"}
        if retranslate and image_ids:
            for image in self.current.images:
                if image.id in image_ids and image.status in {"ready", "review"}:
                    image.status = "queued"
                    image.error = ""
            self.save()
        self._active_job_ids = [
            image.id for image in self.current.images
            if image.status in eligible and (image_ids is None or image.id in image_ids)
        ]
        if not self._active_job_ids:
            return False
        self._active_job_completed = 0
        if hasattr(self.pipeline, "set_request_type"):
            self.pipeline.set_request_type("batch" if image_ids is None else "selected")
        started = self.pipeline.process_project(self.current, set(self._active_job_ids))
        if started:
            APP_STATE.set_busy(True)
            APP_STATE.set_pipeline("analyzing", 0, len(self._active_job_ids), "Preparing translation models…")
        else:
            self._active_job_ids = []
        return started

    def cancel_pipeline(self) -> None:
        self.cancel_active_requests()

    def cancel_active_requests(self) -> bool:
        """Cancel queued/current translation work and queued manual renders."""
        requested = False
        if self.pipeline.running:
            self.pipeline.cancel()
            requested = True
            APP_STATE.set_pipeline(
                "cancelled",
                APP_STATE.progress_current,
                APP_STATE.progress_total,
                "Cancelling after current stage...",
            )
        requested = self.manual_service.cancel_all() or requested
        for request_id in tuple(self._manual_render_contexts):
            requested = self.render_queue.cancel(request_id) or requested
        return requested

    def update_edit(self, image_index: int, group_index: int | str, edit: RegionEdit) -> None:
        if self.current is None:
            return
        payload = self.effective_translation_payload(image_index)
        group = next((item for item in payload["translation_groups"] if str(item["index"]) == str(group_index)), None)
        if group is not None:
            self._capture_edit_corrections(image_index, group, edit)
        self.current.images[image_index].edits[str(group_index)] = edit
        APP_STATE.set_dirty(True)
        self.save()
        self.image_updated.emit(image_index)

    def validate_edit(self, image_index: int, group_index: int | str, edit: RegionEdit) -> None:
        """Validate render-sensitive edit values before they are persisted."""
        if self.current is None:
            raise ValueError("No project is open.")
        if not edit.replace or edit.font_size <= 0:
            return
        from PIL import Image
        from .phase3_cli import prepare_group_fit

        payload = self.effective_translation_payload(image_index)
        group = next((item for item in payload["translation_groups"] if str(item["index"]) == str(group_index)), None)
        if group is None:
            raise ValueError("The selected text block no longer exists.")
        candidate = dict(group)
        candidate.update({
            "translated_text": edit.translated_text or "",
            "font_size_override": edit.font_size,
            "placement_offset": [edit.offset_x, edit.offset_y],
            "font_family": edit.font_family,
            "text_color": edit.color,
            "alignment": edit.alignment,
        })
        with Image.open(self.current.images[image_index].source_path) as opened:
            prepare_group_fit(candidate, opened.size)

    def effective_translation_payload(self, image_index: int) -> dict:
        if self.current is None:
            raise ValueError("No project is open.")
        image = self.current.images[image_index]
        path = Path(image.translation_result)
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = {
                "source": image.source_path, "source_language": image.source_language or "",
                "target_language": self.current.target_language, "source_regions": [], "translation_groups": [],
                "literal_provider": self.current.literal_provider,
                "localization_provider": self.current.localization_provider,
                "localization_style": self.current.localization_style,
                "text_style": self.current.text_style, "bubble_padding": self.current.bubble_padding,
                "max_lines": self.current.max_lines,
            }
        suppressed = set(image.suppressed_auto_group_indices) | {
            index for manual in image.manual_regions
            for index in manual.suppressed_auto_group_indices
        }
        payload["translation_groups"] = [
            group for group in payload["translation_groups"]
            if int(group["index"]) not in suppressed
        ]
        for manual in image.manual_regions:
            x1, y1, x2, y2 = manual.rect
            render_direction = (
                "vertical-rtl" if manual.direction == "vertical-rtl" and (x2 - x1) < 40 else "horizontal-ltr"
            )
            payload["translation_groups"].append({
                "index": manual.key, "manual": True, "manual_id": manual.id,
                "original_text": manual.original_text, "translated_text": manual.translated_text,
                "type": "dialogue", "bubble_type": "dialogue",
                "ocr_confidence": manual.ocr_confidence,
                "manual_rect": [x1, y1, x2, y2],
                "polygon": [[x1, y1], [x2, y1], [x2, y2], [x1, y2]],
                "status": manual.status, "review_reasons": list(manual.review_reasons),
                "member_region_indices": [], "direction": manual.direction,
                "source_direction": manual.direction,
                "render_direction": render_direction,
                "source_polygons": manual.source_polygons, "placement_policy": "exact",
                "source_member_texts": list(manual.source_member_texts),
                "source_language": manual.source_language,
            })
        for group in payload["translation_groups"]:
            edit = image.edits.get(str(group["index"]))
            if edit is None:
                continue
            if edit.original_text is not None:
                group["original_text"] = edit.original_text
            if edit.bubble_type is not None:
                group["bubble_type"] = edit.bubble_type
            if edit.translated_text is not None:
                group["translated_text"] = edit.translated_text
            group.update({
                "editor_replace": edit.replace, "font_size_override": edit.font_size,
                "placement_offset": [edit.offset_x, edit.offset_y], "font_family": edit.font_family,
                "text_color": edit.color, "alignment": edit.alignment,
            })
        return payload

    @staticmethod
    def _group_fingerprint(group: dict) -> str:
        geometry = group.get("source_polygons") or [group.get("polygon", [])]
        payload = json.dumps({"geometry": geometry, "manual_id": group.get("manual_id", "")}, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def ai_subject_id(self, image_index: int, group: dict) -> str:
        if self.current is None:
            return ""
        image = self.current.images[image_index]
        fingerprint = self._group_fingerprint(group)
        subject = image.ai_subject_ids.get(fingerprint)
        if not subject:
            subject = str(uuid4())
            image.ai_subject_ids[fingerprint] = subject
            self.save()
        return subject

    def _existing_ai_subject_id(self, image_index: int, group: dict) -> str:
        if self.current is None:
            return ""
        image = self.current.images[image_index]
        return image.ai_subject_ids.get(self._group_fingerprint(group), "")

    def _is_ai_subject_approved(self, image_index: int, group: dict) -> bool:
        if self.current is None:
            return False
        subject = self._existing_ai_subject_id(image_index, group)
        return bool(subject and subject in self.current.images[image_index].approved_ai_subject_ids)

    def _mark_ai_subjects_approved(self, image_index: int, subject_ids: list[str]) -> None:
        if self.current is None:
            return
        image = self.current.images[image_index]
        known = set(image.approved_ai_subject_ids)
        for subject in subject_ids:
            if subject and subject not in known:
                image.approved_ai_subject_ids.append(subject)
                known.add(subject)
        self._update_image_review_status(image_index)
        self.save()
        self.image_updated.emit(image_index)

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

    @staticmethod
    def ocr_review_reasons(group: dict) -> list[str]:
        text = str(group.get("original_text", "")).strip()
        confidence = float(group.get("ocr_confidence", 0.0) or 0.0)
        reasons: list[str] = []
        if confidence < 0.75:
            reasons.append("low_ocr_confidence")
        if any(char.isascii() and char.isdigit() for char in text):
            reasons.append("digit_like_ocr")
        if not text:
            reasons.append("empty_ocr")
        elif len(text) <= 2 and group.get("bubble_type") not in {"sfx", "sign"}:
            reasons.append("very_short_text")
        if text and not any("\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff" for char in text):
            reasons.append("no_japanese_script")
        for reason in group.get("review_reasons", []):
            reason = str(reason)
            if reason.startswith(("ocr", "low_confidence", "script", "digit")):
                reasons.append(reason)
        return list(dict.fromkeys(reasons))

    def ocr_review_queue(self) -> list[dict]:
        if self.current is None:
            return []
        queue: list[dict] = []
        for image_index, image in enumerate(self.current.images):
            if not image.translation_result or not Path(image.translation_result).is_file():
                continue
            try:
                groups = self.effective_translation_payload(image_index).get("translation_groups", [])
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            for block_index, group in enumerate(groups):
                if self._is_ai_subject_approved(image_index, group):
                    continue
                reasons = self.ocr_review_reasons(group)
                if not reasons:
                    continue
                queue.append({
                    "image_index": image_index,
                    "block_index": block_index,
                    "group_index": group.get("index"),
                    "image_id": image.id,
                    "page": image_index + 1,
                    "text": str(group.get("original_text", "")),
                    "confidence": float(group.get("ocr_confidence", 0.0) or 0.0),
                    "reasons": reasons,
                })
        return sorted(queue, key=lambda item: (item["confidence"], item["page"], str(item["group_index"])))

    def review_issue_queue(self) -> list[dict]:
        if self.current is None:
            return []
        queue: list[dict] = []
        for image_index, image in enumerate(self.current.images):
            if not image.translation_result or not Path(image.translation_result).is_file():
                continue
            try:
                groups = self.effective_translation_payload(image_index).get("translation_groups", [])
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            for block_index, group in enumerate(groups):
                if self._is_ai_subject_approved(image_index, group):
                    continue
                ocr_reasons = set(self.ocr_review_reasons(group))
                reasons = [
                    str(reason) for reason in group.get("review_reasons", [])
                    if str(reason) and str(reason) not in ocr_reasons
                ]
                if group.get("status") == "review" and not reasons and not ocr_reasons:
                    reasons = ["review_required"]
                if not reasons:
                    continue
                queue.append({
                    "image_index": image_index,
                    "block_index": block_index,
                    "group_index": group.get("index"),
                    "image_id": image.id,
                    "page": image_index + 1,
                    "text": str(group.get("translated_text") or group.get("original_text") or ""),
                    "confidence": float(group.get("ocr_confidence", 0.0) or 0.0),
                    "reasons": list(dict.fromkeys(reasons)),
                })
        return sorted(queue, key=lambda item: (item["page"], item["block_index"], str(item["group_index"])))

    def request_manual_region(self, image_index: int, rect: list[int]) -> bool:
        if self.current is None or not (0 <= image_index < len(self.current.images)):
            return False
        image = self.current.images[image_index]
        source_language = resolve_source_language(self.current.source_language, image.source_language)
        manual_engine = str(self.current.localization_provider or "local").strip().lower()
        if manual_engine == "local":
            manual_engine = "marian"
        started = self.manual_service.submit({
            "project_id": self.current.id, "image_id": image.id, "image_index": image_index,
            "source_path": image.source_path, "rect": list(rect), "target": self.current.target_language,
            "source_language": source_language,
            "literal_provider": self.current.literal_provider,
            "localization_provider": self.current.localization_provider,
            "localization_model": self.current.localization_model,
            "localization_style": self.current.localization_style,
            "glossary": dict(self.current.glossary), "max_lines": self.current.max_lines,
            "quality": self.current.quality,
            "translation_engine": manual_engine,
            "translation_fallback_engine": SETTINGS.translation_fallback_engine,
            "allow_local_fallback_for_cloud": True,
            "qwen_model_path": SETTINGS.qwen_model_path,
            "qwen_model_name": SETTINGS.qwen_model_name,
            "provider_models": {
                "groq": SETTINGS.groq_model,
                "gemini": SETTINGS.gemini_model,
                "deepseek": SETTINGS.deepseek_model,
            },
            "ocr_cache_dir": str(self.paths.ocr_cache),
            "ocr_subprocess_enabled": SETTINGS.ocr_subprocess_enabled,
            "ocr_worker_recycle_pages": SETTINGS.ocr_worker_recycle_pages,
            "ocr_worker_memory_limit_mb": SETTINGS.ocr_worker_memory_limit_mb,
            "cache_path": str(self.current.artifacts / "translation_cache.json"),
        })
        if started:
            self.manual_region_started.emit(image_index)
        return started

    def _on_manual_region_succeeded(self, result: dict) -> None:
        if self.current is None or self.current.id != result.get("project_id"):
            return
        image_index = int(result["image_index"])
        if not (0 <= image_index < len(self.current.images)):
            return
        image = self.current.images[image_index]
        if image.id != result.get("image_id"):
            return
        manual = None
        try:
            had_full_translation = bool(image.translation_result and Path(image.translation_result).is_file())
            base = self.effective_translation_payload(image_index)
            result["suppressed_auto_group_indices"] = overlapping_auto_indices(base.get("translation_groups", []), result["rect"])
            request_id = str(result.get("request_id") or "")
            values = {
                key: value for key, value in result.items()
                if key not in {"request_id", "project_id", "image_id", "image_index"}
            }
            manual = ManualRegion(**values)
            image.manual_regions.append(manual)
            subject = self.ai_subject_id(image_index, {
                "manual_id": manual.id,
                "source_polygons": manual.source_polygons,
                "polygon": [[manual.rect[0], manual.rect[1]], [manual.rect[2], manual.rect[1]], [manual.rect[2], manual.rect[3]], [manual.rect[0], manual.rect[3]]],
            })
            HYDRA_AI.capture_correction(
                event_type="region_created", task="bubble", subject_id=subject, project_id=self.current.id,
                image_id=image.id, before={}, after={"rect": manual.rect, "type": "speech"},
                profile=self.current.text_style, source_language="ja", target_language=self.current.target_language,
                confidence=manual.ocr_confidence, page_hash=self._file_hash(Path(image.source_path)),
                input_path=image.source_path, metadata={"manual": True, "direction": manual.direction},
            )
            image.source_language = manual.source_language
            if had_full_translation:
                self._update_image_review_status(image_index)
            else:
                image.status = "partial"
            self.save()
            self._start_manual_region_render(image_index, manual, request_id=request_id)
        except (MemoryError, OSError, ValueError, json.JSONDecodeError, TypeError) as error:
            if manual is not None:
                image.manual_regions = [item for item in image.manual_regions if item.id != manual.id]
                try:
                    self._update_image_review_status(image_index)
                    self.save()
                except OSError:
                    pass
            message = (
                "Could not rerender this page because the original image exceeded available memory. "
                "Close other jobs or use a lower-resolution copy of this page."
                if isinstance(error, MemoryError)
                else f"Could not add manual text box: {error}"
            )
            self.manual_region_failed.emit(image_index, message)

    def _start_manual_region_render(
        self,
        image_index: int,
        manual: ManualRegion,
        *,
        request_id: str = "",
    ) -> None:
        if self.current is None:
            self.manual_region_failed.emit(image_index, "Could not add manual text box: no project is open.")
            return
        image = self.current.images[image_index]
        payload = self.effective_translation_payload(image_index)
        working = self.current.artifacts / "editor"
        working.mkdir(parents=True, exist_ok=True)
        result_path = working / f"{image.id}_translated_en.json"
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        render_dir = self.current.artifacts / image.id
        request_id = request_id or f"manual:{manual.id}"
        self._manual_render_contexts[request_id] = {
            "project_id": self.current.id,
            "image_id": image.id,
            "image_index": image_index,
            "manual_id": manual.id,
            "manual_key": manual.key,
            "source_path": image.source_path,
            "render_dir": str(render_dir),
        }
        self.manual_region_busy_changed.emit(True)
        self.translation_request_state_changed.emit(request_id, "rendering", "Rendering queued")
        request = RenderRequest(
            request_id=request_id,
            project_id=self.current.id,
            image_id=image.id,
            image_index=image_index,
            result_path=result_path,
            render_dir=render_dir,
            source_path=Path(image.source_path),
            reason="manual",
        )
        self.render_queue.submit(request, self._run_editor_render)

    @Slot(str, object)
    def _on_render_queue_completed(self, request_id: str, result: dict) -> None:
        context = self._manual_render_contexts.pop(request_id, None)
        if context is None:
            return
        self.manual_region_busy_changed.emit(bool(self._manual_render_contexts))
        if self.current is None or self.current.id != context.get("project_id"):
            return
        image_index = int(context.get("image_index", -1))
        if not (0 <= image_index < len(self.current.images)):
            return
        image = self.current.images[image_index]
        if image.id != context.get("image_id"):
            return
        source = Path(str(context.get("source_path", image.source_path)))
        render_dir = Path(str(result.get("render_dir") or context.get("render_dir")))
        image.rendered_image = str(render_dir / f"{source.stem}_translated_en.png")
        image.preview_image = str(render_dir / f"{source.stem}_preview.png")
        self.save()
        self.image_updated.emit(image_index)
        self.manual_region_finished.emit(image_index, str(context.get("manual_key", "")))
        self.translation_request_state_changed.emit(request_id, "done", "Done")

    @Slot(str, object)
    def _on_render_queue_failed(self, request_id: str, result: dict) -> None:
        context = self._manual_render_contexts.pop(request_id, None)
        if context is None:
            return
        self.manual_region_busy_changed.emit(bool(self._manual_render_contexts))
        if self.current is None or self.current.id != context.get("project_id"):
            return
        image_index = int(context.get("image_index", -1))
        if not (0 <= image_index < len(self.current.images)):
            return
        image = self.current.images[image_index]
        manual_id = str(context.get("manual_id", ""))
        if manual_id:
            image.manual_regions = [item for item in image.manual_regions if item.id != manual_id]
        self._update_image_review_status(image_index)
        try:
            self.save()
        except OSError:
            pass
        message = self._render_failure_message(RuntimeError(str(result.get("message", ""))))
        self.manual_region_failed.emit(image_index, message)
        self.translation_request_state_changed.emit(request_id, "failed", message)

    @Slot(str)
    def _on_render_queue_cancelled(self, request_id: str) -> None:
        context = self._manual_render_contexts.pop(request_id, None)
        if context is None:
            return
        self.manual_region_busy_changed.emit(bool(self._manual_render_contexts))
        if self.current is not None and self.current.id == context.get("project_id"):
            image_index = int(context.get("image_index", -1))
            if 0 <= image_index < len(self.current.images):
                image = self.current.images[image_index]
                if image.id == context.get("image_id"):
                    manual_id = str(context.get("manual_id", ""))
                    image.manual_regions = [
                        item for item in image.manual_regions
                        if item.id != manual_id
                    ]
                    self._update_image_review_status(image_index)
                    try:
                        self.save()
                    except OSError:
                        pass
        self.translation_request_state_changed.emit(
            request_id, "cancelled", "Cancelled before rendering",
        )

    def _on_manual_region_failed(self, result: dict) -> None:
        if self.current is None or self.current.id != result.get("project_id"):
            return
        self.manual_region_failed.emit(int(result.get("image_index", -1)), result.get("message", "Manual translation failed."))

    def delete_manual_region(self, image_index: int, key: str) -> bool:
        if self.current is None or not (0 <= image_index < len(self.current.images)) or not key.startswith("manual:"):
            return False
        image = self.current.images[image_index]
        removed_region = next((manual for manual in image.manual_regions if manual.key == key), None)
        before = len(image.manual_regions)
        previous_manual_regions = list(image.manual_regions)
        previous_edit = image.edits.get(key)
        image.manual_regions = [manual for manual in image.manual_regions if manual.key != key]
        if len(image.manual_regions) == before:
            return False
        if removed_region is not None:
            group = {"manual_id": removed_region.id, "source_polygons": removed_region.source_polygons,
                     "polygon": [[removed_region.rect[0], removed_region.rect[1]], [removed_region.rect[2], removed_region.rect[1]], [removed_region.rect[2], removed_region.rect[3]], [removed_region.rect[0], removed_region.rect[3]]]}
            HYDRA_AI.capture_correction(
                event_type="region_deleted", task="bubble", subject_id=self.ai_subject_id(image_index, group),
                project_id=self.current.id, image_id=image.id, before={"rect": removed_region.rect, "type": "speech"}, after={},
                profile=self.current.text_style, source_language="ja", target_language=self.current.target_language,
                confidence=removed_region.ocr_confidence, page_hash=self._file_hash(Path(image.source_path)), input_path=image.source_path,
                metadata={"manual": True},
            )
        image.edits.pop(key, None)
        self._update_image_review_status(image_index)
        try:
            self.save()
            self.rerender_image(image_index)
        except (MemoryError, OSError, ValueError, json.JSONDecodeError, TypeError) as error:
            image.manual_regions = previous_manual_regions
            if previous_edit is not None:
                image.edits[key] = previous_edit
            self._update_image_review_status(image_index)
            self.save()
            self.manual_region_failed.emit(image_index, self._render_failure_message(error))
            return False
        return True

    def suppress_auto_region(self, image_index: int, group_index: int) -> bool:
        if self.current is None or not (0 <= image_index < len(self.current.images)):
            return False
        image = self.current.images[image_index]
        base = json.loads(Path(image.translation_result).read_text(encoding="utf-8"))
        removed_group = next((group for group in base.get("translation_groups", []) if int(group["index"]) == group_index), None)
        if group_index not in {int(group["index"]) for group in base.get("translation_groups", [])}:
            return False
        if group_index not in image.suppressed_auto_group_indices:
            image.suppressed_auto_group_indices.append(group_index)
            image.suppressed_auto_group_indices.sort()
        if removed_group is not None:
            HYDRA_AI.capture_correction(
                event_type="region_deleted", task="bubble", subject_id=self.ai_subject_id(image_index, removed_group),
                project_id=self.current.id, image_id=image.id, before={"polygon": removed_group.get("polygon"), "type": removed_group.get("bubble_type", "speech")}, after={},
                profile=self.current.text_style, source_language="ja", target_language=self.current.target_language,
                confidence=float(removed_group.get("ocr_confidence", 0.0)), page_hash=self._file_hash(Path(image.source_path)), input_path=image.source_path,
                metadata={"manual": False, "group_index": group_index},
            )
        edit_key = str(group_index)
        previous_edit = image.edits.get(edit_key)
        image.edits.pop(edit_key, None)
        self._update_image_review_status(image_index)
        try:
            self.save()
            self.rerender_image(image_index)
        except (MemoryError, OSError, ValueError, json.JSONDecodeError, TypeError) as error:
            if group_index in image.suppressed_auto_group_indices:
                image.suppressed_auto_group_indices.remove(group_index)
            if previous_edit is not None:
                image.edits[edit_key] = previous_edit
            self._update_image_review_status(image_index)
            self.save()
            self.manual_region_failed.emit(image_index, self._render_failure_message(error))
            return False
        return True

    def restore_auto_regions(self, image_index: int) -> bool:
        if self.current is None or not (0 <= image_index < len(self.current.images)):
            return False
        image = self.current.images[image_index]
        if not image.suppressed_auto_group_indices:
            return False
        previous = list(image.suppressed_auto_group_indices)
        image.suppressed_auto_group_indices.clear()
        self._update_image_review_status(image_index)
        try:
            self.save()
            self.rerender_image(image_index)
        except (MemoryError, OSError, ValueError, json.JSONDecodeError, TypeError) as error:
            image.suppressed_auto_group_indices = previous
            self._update_image_review_status(image_index)
            self.save()
            self.manual_region_failed.emit(image_index, self._render_failure_message(error))
            return False
        return True

    def _update_image_review_status(self, image_index: int) -> None:
        if self.current is None:
            return
        image = self.current.images[image_index]
        if not image.translation_result or not Path(image.translation_result).is_file():
            image.status = "partial" if image.manual_regions else "queued"
            return
        groups = self.effective_translation_payload(image_index).get("translation_groups", [])
        has_unapproved_review = False
        for group in groups:
            if self._is_ai_subject_approved(image_index, group):
                continue
            ocr_reasons = set(self.ocr_review_reasons(group))
            review_reasons = [
                str(reason) for reason in group.get("review_reasons", [])
                if str(reason) and str(reason) not in ocr_reasons
            ]
            if ocr_reasons or review_reasons or group.get("status") == "review":
                has_unapproved_review = True
                break
        image.status = "review" if has_unapproved_review else "ready"

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
        self.save()
        self.image_updated.emit(image_index)
        return Path(image.rendered_image)

    @staticmethod
    def _run_editor_render(result_path: Path, render_dir: Path, policy: str = "complete") -> list[str]:
        if getattr(sys, "frozen", False):
            from .phase3_cli import run as render_phase3
            render_phase3(result_path, render_dir, policy=policy)
            return []
        command = [
            sys.executable,
            "-m",
            "hydra_manga_tl.phase3_cli",
            str(result_path),
            "--output",
            str(render_dir),
            "--policy",
            policy,
        ]
        completed = subprocess.run(
            command,
            cwd=str(Path(__file__).resolve().parents[1]),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            details = (completed.stderr or completed.stdout or "").strip().splitlines()
            message = details[-1] if details else f"renderer exited with code {completed.returncode}"
            raise RuntimeError(message)
        return [
            line.strip()
            for line in (completed.stdout or completed.stderr or "").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _render_failure_message(error: BaseException) -> str:
        if isinstance(error, MemoryError):
            return (
                "Could not rerender this page because the original image exceeded available memory. "
                "Close other jobs or use a lower-resolution copy of this page."
            )
        return f"Could not rerender this page: {error}"

    def export(self, destination: Path, *, mode: str = "translated", image_format: str = "png") -> int:
        if self.current is None:
            return 0
        count = export_images(self.current, destination, mode=mode, image_format=image_format)
        APP_STATE.set_export(str(destination.resolve()), count)
        return count

    def export_archive(self, destination: Path, *, mode: str = "translated", image_format: str = "png", archive_format: str = "zip") -> Path | None:
        if self.current is None:
            return None
        archive = export_archive(self.current, destination, mode=mode, image_format=image_format, archive_format=archive_format)
        APP_STATE.set_export(str(archive.resolve()), 1)
        return archive

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
        return entries[:5]

    def recent_projects(self) -> list[Path]:
        return [path for path, _ in self._recent_entries()]

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
            summaries.append(RecentProjectSummary(path, name, source, target, len(images), last_opened))
        return summaries

    def forget_recent_project(self, project_file: Path) -> None:
        remaining = [
            {"path": str(path), "last_opened": last_opened}
            for path, last_opened in self._recent_entries()
            if path.resolve() != project_file.resolve()
        ]
        self.paths.recent.parent.mkdir(parents=True, exist_ok=True)
        self.paths.recent.write_text(json.dumps(remaining, indent=2), encoding="utf-8")

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
        self.paths.recent.write_text(json.dumps(remembered[:5], indent=2), encoding="utf-8")

    def _find_image(self, image_id: str):
        if self.current is None:
            return -1, None
        for index, image in enumerate(self.current.images):
            if image.id == image_id:
                return index, image
        return -1, None

    def _on_progress(self, image_id: str, stage: str, current: int, total: int, message: str) -> None:
        index, image = self._find_image(image_id)
        if image is not None:
            image.status = stage
            self.image_updated.emit(index)
        APP_STATE.set_pipeline(stage, current, total, message)
        self.save()

    def _on_image_finished(self, image_id: str, result: dict) -> None:
        index, image = self._find_image(image_id)
        if image is None:
            return
        for key, value in result.items():
            setattr(image, key, value)
        self._update_image_review_status(index)
        self.save()
        if image.manual_regions or image.edits or image.suppressed_auto_group_indices:
            try:
                self.rerender_image(index)
            except (OSError, ValueError, json.JSONDecodeError) as error:
                image.status = "review"
                image.error = f"Automatic translation completed, but editor overrides could not be rendered: {error}"
                self.save()
        self.image_updated.emit(index)
        if image_id in self._active_job_ids:
            self._active_job_completed += 1
            APP_STATE.set_pipeline(
                "complete", self._active_job_completed, len(self._active_job_ids),
                f"Completed {Path(image.source_path).name}",
            )
        if APP_STATE.selected_image < 0:
            APP_STATE.select(index)

    def _on_image_failed(self, image_id: str, message: str) -> None:
        index, image = self._find_image(image_id)
        if image is not None:
            image.status = "failed"
            image.error = message
            self.image_updated.emit(index)
        APP_STATE.report_error(message)
        self.save()
        if image_id in self._active_job_ids:
            self._active_job_completed += 1
            failed_name = Path(image.source_path).name if image is not None else "pipeline initialization"
            APP_STATE.set_pipeline(
                "failed", self._active_job_completed, len(self._active_job_ids),
                f"Failed: {failed_name}",
            )

    def _on_completed(self, cancelled: bool) -> None:
        if cancelled and self.current is not None:
            for image in self.current.images:
                if image.id in self._active_job_ids and image.status in {"pending", "queued", "partial", "preprocessing", "OCR", "ocr", "translating", "localizing", "rendering", "reconstructing", "analyzing", "review"}:
                    image.status = "cancelled"
        APP_STATE.set_busy(False)
        total = len(self._active_job_ids)
        completed = self._active_job_completed if cancelled else total
        APP_STATE.set_pipeline(
            "cancelled" if cancelled else "ready", completed, total,
            "Cancelled" if cancelled else "Translation complete",
        )
        self.save()
        APP_STATE.refresh_project()
        self.pipeline_finished.emit(cancelled)
        self._active_job_ids = []


WORKSPACE = WorkspaceManager()
