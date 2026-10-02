"""Manual-region orchestration for workspace projects."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Slot

from hydra_manga_tl.core.ai_bridge import HYDRA_AI
from hydra_manga_tl.core.language import resolve_source_language
from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.project.manual_region import (
    manual_region_user_message,
    overlapping_auto_indices,
    polygon_bounding_rect,
    rect_to_polygon,
)
from hydra_manga_tl.project.model import ManualRegion
from hydra_manga_tl.translation.engines import PageDialogue, PageTranslation
from hydra_manga_tl.translation.memory import learn_validated_page
from hydra_manga_tl.translation.requests import RenderRequest


LOGGER = logging.getLogger(__name__)


class ManualRegionWorkspaceMixin:
    """Provide manual text-box requests, rendering, and status updates."""
    def request_manual_region(self, image_index: int, region: list[int] | list[list[int]]) -> bool:
        if self.current is None or not (0 <= image_index < len(self.current.images)):
            return False
        image = self.current.images[image_index]
        if len(region) == 4 and all(not isinstance(value, (list, tuple)) for value in region):
            rect = [int(value) for value in region]
            polygon = rect_to_polygon(rect)
        else:
            polygon = [[int(point[0]), int(point[1])] for point in region]  # type: ignore[index]
            rect = polygon_bounding_rect(polygon) or []
        if len(rect) != 4:
            return False
        source_language = resolve_source_language(self.current.source_language, image.source_language)
        manual_engine = str(self.current.localization_provider or "local").strip().lower()
        if manual_engine == "local":
            manual_engine = "marian"
        LOGGER.info(
            "Manual box: request image=%s rect=%s polygon_points=%d engine=%s source_language=%s",
            image_index,
            rect,
            len(polygon),
            manual_engine,
            source_language,
        )
        started = self.manual_service.submit({
            "project_id": self.current.id, "image_id": image.id, "image_index": image_index,
            "source_path": image.source_path, "rect": list(rect), "polygon": polygon,
            "target": self.current.target_language,
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
            "qwen_model_name": (self.current.localization_model if manual_engine == "qwen" else "") or SETTINGS.qwen_model_name,
            "provider_models": {
                "groq": SETTINGS.groq_model,
                "gemini": SETTINGS.gemini_model,
                "deepseek": SETTINGS.deepseek_model,
                "openai": SETTINGS.openai_model,
                "openai_compatible": SETTINGS.openai_compatible_model,
                **({manual_engine: self.current.localization_model} if self.current.localization_model else {}),
            },
            "provider_base_urls": {
                "openai_compatible": SETTINGS.openai_compatible_base_url,
            },
            "ocr_cache_dir": str(self.paths.ocr_cache),
            "ocr_subprocess_enabled": SETTINGS.ocr_subprocess_enabled,
            "ocr_worker_recycle_pages": SETTINGS.ocr_worker_recycle_pages,
            "ocr_worker_memory_limit_mb": SETTINGS.ocr_worker_memory_limit_mb,
            "translation_memory_enabled": SETTINGS.translation_memory_enabled,
            "translation_memory_auto_learn": (
                SETTINGS.translation_memory_auto_learn
            ),
            "translation_memory_prefer_verified": (
                SETTINGS.translation_memory_prefer_verified
            ),
            "cache_path": str(self.current.artifacts / "translation_cache.json"),
        })
        if started:
            LOGGER.info("Manual box: started image=%s", image_index)
            self.manual_region_started.emit(image_index)
        else:
            LOGGER.info("Manual box: not started image=%s; service is busy", image_index)
        return started

    def request_title_region(self, image_index: int, region: list[int] | list[list[int]]) -> bool:
        if self.current is None or not (0 <= image_index < len(self.current.images)):
            return False
        image = self.current.images[image_index]
        if len(region) == 4 and all(not isinstance(value, (list, tuple)) for value in region):
            rect = [int(value) for value in region]
            polygon = rect_to_polygon(rect)
        else:
            polygon = [[int(point[0]), int(point[1])] for point in region]  # type: ignore[index]
            rect = polygon_bounding_rect(polygon) or []
        if len(rect) != 4:
            return False
        source_language = resolve_source_language(self.current.source_language, image.source_language)
        manual_engine = str(self.current.localization_provider or "local").strip().lower()
        if manual_engine == "local":
            manual_engine = "marian"
        request_id = f"title:{uuid4()}"
        LOGGER.info(
            "Title reconstruction region: OCR/translation requested image=%s rect=%s polygon_points=%d engine=%s source_language=%s",
            image_index,
            rect,
            len(polygon),
            manual_engine,
            source_language,
        )
        started = self.manual_service.submit({
            "request_id": request_id,
            "project_id": self.current.id, "image_id": image.id, "image_index": image_index,
            "source_path": image.source_path, "rect": list(rect), "polygon": polygon,
            "target": self.current.target_language,
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
            "qwen_model_name": (self.current.localization_model if manual_engine == "qwen" else "") or SETTINGS.qwen_model_name,
            "provider_models": {
                "groq": SETTINGS.groq_model,
                "gemini": SETTINGS.gemini_model,
                "deepseek": SETTINGS.deepseek_model,
                "openai": SETTINGS.openai_model,
                "openai_compatible": SETTINGS.openai_compatible_model,
                **({manual_engine: self.current.localization_model} if self.current.localization_model else {}),
            },
            "provider_base_urls": {
                "openai_compatible": SETTINGS.openai_compatible_base_url,
            },
            "ocr_cache_dir": str(self.paths.ocr_cache),
            "ocr_subprocess_enabled": SETTINGS.ocr_subprocess_enabled,
            "ocr_worker_recycle_pages": SETTINGS.ocr_worker_recycle_pages,
            "ocr_worker_memory_limit_mb": SETTINGS.ocr_worker_memory_limit_mb,
            "translation_memory_enabled": SETTINGS.translation_memory_enabled,
            "translation_memory_auto_learn": (
                SETTINGS.translation_memory_auto_learn
            ),
            "translation_memory_prefer_verified": (
                SETTINGS.translation_memory_prefer_verified
            ),
            "cache_path": str(self.current.artifacts / "translation_cache.json"),
            "bubble_type": "title",
            "render_mode": "art_text",
            "title_composition": {},
            "title_reconstruction": {"manual_reconstruction": True},
            "style_profile": None,
        })
        if started:
            LOGGER.info("Title reconstruction region: OCR/translation started image=%s request=%s", image_index, request_id)
            self.manual_region_started.emit(image_index)
        else:
            LOGGER.info("Title reconstruction region: not started image=%s; service is busy", image_index)
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
            LOGGER.info(
                "Manual box: OCR/translation succeeded image=%s rect=%s status=%s",
                image_index,
                result.get("rect"),
                result.get("status", ""),
            )
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
                "polygon": manual.polygon,
            })
            HYDRA_AI.capture_correction(
                event_type="region_created", task="bubble", subject_id=subject, project_id=self.current.id,
                image_id=image.id, before={}, after={"rect": manual.rect, "polygon": manual.polygon, "type": manual.bubble_type},
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
            LOGGER.exception("Manual box: failed while saving or rendering image=%s", image_index)
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
        LOGGER.info(
            "Manual box: render queued image=%s manual=%s key=%s",
            image_index,
            manual.id,
            manual.key,
        )
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
        self._persist_rendered_style_profiles(image_index, render_dir)
        manual = next(
            (
                item for item in image.manual_regions
                if item.id == str(context.get("manual_id", ""))
            ),
            None,
        )
        if (
            manual is not None
            and manual.status == "translated"
            and not manual.review_reasons
            and SETTINGS.translation_memory_enabled
            and SETTINGS.translation_memory_auto_learn
            and manual.translation_memory_units
        ):
            page = PageDialogue(
                source_language=manual.source_language,
                target_language=self.current.target_language,
                dialogue=[
                    {
                        "id": str(unit.get("id", "")),
                        "text": str(unit.get("source_text", "")),
                        "region_type": str(
                            unit.get("region_type", manual.bubble_type)
                        ),
                        "source_text_hash": str(
                            unit.get("source_text_hash", "")
                        ),
                        "source_region_hash": unit.get("source_region_hash"),
                    }
                    for unit in manual.translation_memory_units
                ],
            )
            page_result = PageTranslation(
                source_language=manual.source_language,
                target_language=self.current.target_language,
                translations=[
                    {
                        "id": str(unit.get("id", "")),
                        "text": str(unit.get("translated_text", "")),
                        "translation_source": str(
                            unit.get("translation_source", "provider")
                        ),
                        "provider_id": str(unit.get("provider_id", "")),
                        "tm_entry_id": unit.get("tm_entry_id"),
                    }
                    for unit in manual.translation_memory_units
                ],
            )
            learn_validated_page(
                page,
                page_result,
                project_id=self.current.id,
            )
        self.save()
        self.image_updated.emit(image_index)
        LOGGER.info(
            "Manual box: render complete image=%s manual_key=%s output=%s",
            image_index,
            context.get("manual_key", ""),
            image.rendered_image,
        )
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
        LOGGER.info(
            "Manual box: render failed image=%s manual_id=%s message=%s",
            image_index,
            manual_id,
            message,
        )
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
        LOGGER.info("Manual box: render cancelled request_id=%s", request_id)
        self.translation_request_state_changed.emit(
            request_id, "cancelled", "Cancelled before rendering",
        )

    def _on_manual_region_failed(self, result: dict) -> None:
        if self.current is None or self.current.id != result.get("project_id"):
            return
        message = manual_region_user_message(result.get("message", "Manual translation failed."))
        LOGGER.info(
            "Manual box: failed image=%s message=%s",
            result.get("image_index", -1),
            result.get("message", "Manual translation failed."),
        )
        self.manual_region_failed.emit(int(result.get("image_index", -1)), message)

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


