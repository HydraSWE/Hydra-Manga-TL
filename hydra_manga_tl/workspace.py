"""Project, import, editing, pipeline, and export orchestration."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

from PySide6.QtCore import QObject, Signal

from .cli import SUPPORTED, discover, image_path_sort_key
from .editor_project import RegionEdit
from .language import resolve_source_language
from .manual_region import ManualRegionService, overlapping_auto_indices
from .paths import PATHS
from .pipeline import PipelineService
from .project import MangaProject, ManualRegion
from .settings import SETTINGS
from .state import APP_STATE


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

    def __init__(self, paths=PATHS, pipeline: PipelineService | None = None, manual_service: ManualRegionService | None = None) -> None:
        super().__init__()
        self.paths = paths
        self.pipeline = pipeline or PipelineService()
        self.manual_service = manual_service or ManualRegionService()
        self.current: MangaProject | None = None
        self._active_job_ids: list[str] = []
        self._active_job_completed = 0
        self.pipeline.progress.connect(self._on_progress)
        self.pipeline.image_finished.connect(self._on_image_finished)
        self.pipeline.image_failed.connect(self._on_image_failed)
        self.pipeline.completed.connect(self._on_completed)
        self.manual_service.succeeded.connect(self._on_manual_region_succeeded)
        self.manual_service.failed.connect(self._on_manual_region_failed)
        self.manual_service.busy_changed.connect(self.manual_region_busy_changed)

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
        self.current = project
        APP_STATE.set_project(project)
        if project.images:
            APP_STATE.select(min(project.selected_image, len(project.images) - 1), 0)
        APP_STATE.set_dirty(False)
        self._remember(project.project_file)
        self.project_opened.emit(project)

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
        eligible = {"queued", "partial", "failed", "cancelled"}
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
        started = self.pipeline.process_project(self.current, set(self._active_job_ids))
        if started:
            APP_STATE.set_busy(True)
            APP_STATE.set_pipeline("analyzing", 0, len(self._active_job_ids), "Preparing translation models…")
        else:
            self._active_job_ids = []
        return started

    def cancel_pipeline(self) -> None:
        self.pipeline.cancel()
        APP_STATE.set_pipeline("cancelled", APP_STATE.progress_current, APP_STATE.progress_total, "Cancelling after current stage...")

    def update_edit(self, image_index: int, group_index: int | str, edit: RegionEdit) -> None:
        if self.current is None:
            return
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
                "ocr_confidence": manual.ocr_confidence,
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
            if edit.translated_text is not None:
                group["translated_text"] = edit.translated_text
            group.update({
                "editor_replace": edit.replace, "font_size_override": edit.font_size,
                "placement_offset": [edit.offset_x, edit.offset_y], "font_family": edit.font_family,
                "text_color": edit.color, "alignment": edit.alignment,
            })
        return payload

    def request_manual_region(self, image_index: int, rect: list[int]) -> bool:
        if self.current is None or not (0 <= image_index < len(self.current.images)):
            return False
        image = self.current.images[image_index]
        source_language = resolve_source_language(self.current.source_language, image.source_language)
        started = self.manual_service.submit({
            "project_id": self.current.id, "image_id": image.id, "image_index": image_index,
            "source_path": image.source_path, "rect": list(rect), "target": self.current.target_language,
            "source_language": source_language,
            "literal_provider": self.current.literal_provider,
            "localization_provider": self.current.localization_provider,
            "localization_model": self.current.localization_model,
            "localization_style": self.current.localization_style,
            "glossary": dict(self.current.glossary), "max_lines": self.current.max_lines,
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
            values = {key: value for key, value in result.items() if key not in {"project_id", "image_id", "image_index"}}
            manual = ManualRegion(**values)
            image.manual_regions.append(manual)
            image.source_language = manual.source_language
            if had_full_translation:
                self._update_image_review_status(image_index)
            else:
                image.status = "partial"
            self.save()
            self.rerender_image(image_index)
            self.manual_region_finished.emit(image_index, manual.key)
        except (OSError, ValueError, json.JSONDecodeError, TypeError) as error:
            if manual is not None:
                image.manual_regions = [item for item in image.manual_regions if item.id != manual.id]
                try:
                    self._update_image_review_status(image_index)
                    self.save()
                except OSError:
                    pass
            self.manual_region_failed.emit(image_index, f"Could not add manual text box: {error}")

    def _on_manual_region_failed(self, result: dict) -> None:
        if self.current is None or self.current.id != result.get("project_id"):
            return
        self.manual_region_failed.emit(int(result.get("image_index", -1)), result.get("message", "Manual translation failed."))

    def delete_manual_region(self, image_index: int, key: str) -> bool:
        if self.current is None or not (0 <= image_index < len(self.current.images)) or not key.startswith("manual:"):
            return False
        image = self.current.images[image_index]
        before = len(image.manual_regions)
        image.manual_regions = [manual for manual in image.manual_regions if manual.key != key]
        if len(image.manual_regions) == before:
            return False
        image.edits.pop(key, None)
        self._update_image_review_status(image_index)
        self.save()
        self.rerender_image(image_index)
        return True

    def suppress_auto_region(self, image_index: int, group_index: int) -> bool:
        if self.current is None or not (0 <= image_index < len(self.current.images)):
            return False
        image = self.current.images[image_index]
        base = json.loads(Path(image.translation_result).read_text(encoding="utf-8"))
        if group_index not in {int(group["index"]) for group in base.get("translation_groups", [])}:
            return False
        if group_index not in image.suppressed_auto_group_indices:
            image.suppressed_auto_group_indices.append(group_index)
            image.suppressed_auto_group_indices.sort()
        image.edits.pop(str(group_index), None)
        self._update_image_review_status(image_index)
        self.save()
        self.rerender_image(image_index)
        return True

    def restore_auto_regions(self, image_index: int) -> bool:
        if self.current is None or not (0 <= image_index < len(self.current.images)):
            return False
        image = self.current.images[image_index]
        if not image.suppressed_auto_group_indices:
            return False
        image.suppressed_auto_group_indices.clear()
        self._update_image_review_status(image_index)
        self.save()
        self.rerender_image(image_index)
        return True

    def _update_image_review_status(self, image_index: int) -> None:
        if self.current is None:
            return
        image = self.current.images[image_index]
        if not image.translation_result or not Path(image.translation_result).is_file():
            image.status = "partial" if image.manual_regions else "queued"
            return
        groups = self.effective_translation_payload(image_index).get("translation_groups", [])
        image.status = "review" if any(group.get("status") == "review" for group in groups) else "ready"

    def shutdown(self) -> None:
        self.manual_service.shutdown()

    def rerender_image(self, image_index: int) -> Path:
        if self.current is None:
            raise ValueError("No project is open.")
        from .phase3_cli import run as render_phase3
        image = self.current.images[image_index]
        payload = self.effective_translation_payload(image_index)
        working = self.current.artifacts / "editor"
        working.mkdir(parents=True, exist_ok=True)
        result_path = working / f"{image.id}_translated_en.json"
        result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        render_dir = self.current.artifacts / image.id
        render_phase3(result_path, render_dir, policy="complete")
        source = Path(image.source_path)
        image.rendered_image = str(render_dir / f"{source.stem}_translated_en.png")
        image.preview_image = str(render_dir / f"{source.stem}_preview.png")
        self.save()
        self.image_updated.emit(image_index)
        return Path(image.rendered_image)

    def export(self, destination: Path) -> int:
        if self.current is None:
            return 0
        count = 0
        for image in self.current.images:
            source = Path(image.rendered_image)
            if not source.is_file():
                continue
            target = destination / Path(image.relative_path).with_suffix(".png")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            count += 1
        APP_STATE.set_export(str(destination.resolve()), count)
        return count

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
                if image.id in self._active_job_ids and image.status in {"queued", "partial", "ocr", "translating", "localizing", "reconstructing", "analyzing"}:
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
