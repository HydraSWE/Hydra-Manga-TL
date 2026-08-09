"""Workspace editor state, text layout, and effective payload behavior."""

from __future__ import annotations

from dataclasses import asdict
import json
import logging
import math
from pathlib import Path
from typing import Callable

from hydra_manga_tl.core.normalization import normalize_global_text
from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.core.state import APP_STATE
from hydra_manga_tl.project.editor import RegionEdit
from hydra_manga_tl.project.manual_region import rect_to_polygon
from hydra_manga_tl.project.model import ManualRegion


LOGGER = logging.getLogger(__name__)


def _box_text_layout(box: list[int], angle: float | None = None) -> dict:
    x1, y1, x2, y2 = [int(value) for value in box]
    layout = {
        "x": x1,
        "y": y1,
        "width": max(1, x2 - x1),
        "height": max(1, y2 - y1),
    }
    if angle is not None:
        layout["angle"] = float(angle)
    return layout


def _polygon_box(polygon: list) -> list[int] | None:
    if not polygon:
        return None
    try:
        xs = [int(point[0]) for point in polygon]
        ys = [int(point[1]) for point in polygon]
    except (TypeError, ValueError, IndexError):
        return None
    return [min(xs), min(ys), max(xs), max(ys)]




class WorkspaceEditorMixin:
    """Provide editor persistence and payload composition to WorkspaceManager."""

    def update_edit(self, image_index: int, group_index: int | str, edit: RegionEdit) -> None:
        if self.current is None:
            return
        payload = self.effective_translation_payload(image_index)
        group = next((item for item in payload["translation_groups"] if str(item["index"]) == str(group_index)), None)
        if group is not None:
            self._capture_edit_corrections(image_index, group, edit)
            self._learn_user_translation_edit(image_index, group, edit)
        self.current.images[image_index].edits[str(group_index)] = edit
        APP_STATE.set_dirty(True)
        self.save()
        self.image_updated.emit(image_index)

    def update_edits_batch(self, image_index: int, edits_map: dict[int | str, RegionEdit]) -> None:
        if self.current is None or not edits_map:
            return
        payload = self.effective_translation_payload(image_index)
        groups_by_id = {str(item["index"]): item for item in payload.get("translation_groups", [])}
        image = self.current.images[image_index]
        for group_index, edit in edits_map.items():
            group_key = str(group_index)
            group = groups_by_id.get(group_key)
            if group is not None:
                self._capture_edit_corrections(image_index, group, edit)
                self._learn_user_translation_edit(image_index, group, edit)
            image.edits[group_key] = edit
        APP_STATE.set_dirty(True)
        self.save()
        self.image_updated.emit(image_index)

    def restore_edit(
        self,
        image_index: int,
        group_index: int | str,
        edit: RegionEdit | None,
        *,
        rerender: bool = True,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        if self.current is None or not (0 <= image_index < len(self.current.images)):
            raise ValueError("No page is available for editor history.")
        image = self.current.images[image_index]
        group_key = str(group_index)
        if edit is None:
            image.edits.pop(group_key, None)
        else:
            image.edits[group_key] = RegionEdit(**asdict(edit))
        self._update_image_review_status(image_index)
        APP_STATE.set_dirty(True)
        self.save()
        if rerender:
            self.rerender_image(image_index, log_callback=log_callback)
        self.image_updated.emit(image_index)

    def capture_editor_state(self, image_index: int) -> dict:
        if self.current is None or not (0 <= image_index < len(self.current.images)):
            raise ValueError("No page is available for editor history.")
        image = self.current.images[image_index]
        return {
            "status": image.status,
            "error": image.error,
            "source_language": image.source_language,
            "rendered_image": image.rendered_image,
            "preview_image": image.preview_image,
            "edits": {
                str(key): asdict(edit)
                for key, edit in image.edits.items()
            },
            "manual_regions": [
                asdict(region)
                for region in image.manual_regions
            ],
            "suppressed_auto_group_indices": list(image.suppressed_auto_group_indices),
            "approved_ai_subject_ids": list(image.approved_ai_subject_ids),
            "reading_order": list(image.reading_order),
        }

    def restore_editor_state(
        self,
        image_index: int,
        state: dict,
        *,
        rerender: bool = True,
        log_callback: Callable[[str], None] | None = None,
    ) -> None:
        if self.current is None or not (0 <= image_index < len(self.current.images)):
            raise ValueError("No page is available for editor history.")
        image = self.current.images[image_index]
        image.status = str(state.get("status", image.status))
        image.error = str(state.get("error", image.error))
        image.source_language = str(state.get("source_language", image.source_language))
        image.rendered_image = str(state.get("rendered_image", image.rendered_image))
        image.preview_image = str(state.get("preview_image", image.preview_image))
        image.edits = {
            str(key): RegionEdit(**dict(value))
            for key, value in dict(state.get("edits", {})).items()
            if isinstance(value, dict)
        }
        image.manual_regions = [
            ManualRegion(**dict(value))
            for value in list(state.get("manual_regions", []))
            if isinstance(value, dict)
        ]
        image.suppressed_auto_group_indices = [
            int(value)
            for value in state.get("suppressed_auto_group_indices", [])
        ]
        image.approved_ai_subject_ids = [
            str(value)
            for value in state.get("approved_ai_subject_ids", [])
        ]
        image.reading_order = [
            str(value)
            for value in state.get("reading_order", [])
        ]
        self._update_image_review_status(image_index)
        APP_STATE.set_dirty(True)
        self.save()
        if rerender and (
            image.translation_result
            or image.manual_regions
            or image.edits
            or image.suppressed_auto_group_indices
        ):
            self.rerender_image(image_index, log_callback=log_callback)
        self.image_updated.emit(image_index)

    def update_text_layout(self, image_index: int, group_index: int | str, layout: dict) -> None:
        if self.current is None:
            raise ValueError("No project is open.")
        image = self.current.images[image_index]
        group_key = str(group_index)
        previous = image.edits.get(group_key)
        edit = RegionEdit(**asdict(previous)) if previous is not None else RegionEdit()
        try:
            x = int(layout["x"])
            y = int(layout["y"])
            width = int(layout["width"])
            height = int(layout["height"])
            angle = float(layout.get("angle", 0) or 0)
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("Text layout must include integer x, y, width, and height.") from error
        if width < 20 or height < 12:
            raise ValueError("Text layout is too small.")
        from PIL import Image
        with Image.open(image.source_path) as opened:
            image_width, image_height = opened.size
        if x < 0 or y < 0 or x + width > image_width or y + height > image_height:
            raise ValueError("Text layout must stay inside the page.")
        edit.layout_x = x
        edit.layout_y = y
        edit.layout_width = width
        edit.layout_height = height
        edit.layout_angle = angle
        edit.font_size = 0
        image.edits[group_key] = edit
        try:
            LOGGER.info(
                "Text layout: applying image=%s group=%s box=(%d,%d %dx%d), font_size=Auto",
                image_index,
                group_key,
                x,
                y,
                width,
                height,
            )
            self.save()
            self.rerender_image(image_index)
        except (MemoryError, OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError):
            if previous is None:
                image.edits.pop(group_key, None)
            else:
                image.edits[group_key] = previous
            self.save()
            raise

    def validate_edit(self, image_index: int, group_index: int | str, edit: RegionEdit) -> None:
        """Validate render-sensitive edit values before they are persisted."""
        if self.current is None:
            raise ValueError("No project is open.")
        if not edit.replace or edit.font_size <= 0:
            return
        from PIL import Image
        from hydra_manga_tl.phase.phase3 import prepare_group_fit

        payload = self.effective_translation_payload(image_index)
        group = next((item for item in payload["translation_groups"] if str(item["index"]) == str(group_index)), None)
        if group is None:
            raise ValueError("The selected text block no longer exists.")
        candidate = dict(group)
        candidate.update({
            "translated_text": normalize_global_text(edit.translated_text or ""),
            "font_size_override": edit.font_size,
            "placement_offset": [edit.offset_x, edit.offset_y],
            "font_family": edit.font_family,
            "text_color": edit.color,
            "alignment": edit.alignment,
        })
        if all(value is not None for value in (edit.layout_x, edit.layout_y, edit.layout_width, edit.layout_height)):
            candidate["text_layout"] = {
                "x": edit.layout_x, "y": edit.layout_y,
                "width": edit.layout_width, "height": edit.layout_height,
            }
        with Image.open(self.current.images[image_index].source_path) as opened:
            prepare_group_fit(candidate, opened.size)

    @staticmethod
    def _default_text_layout(group: dict, image_size: tuple[int, int] | None = None) -> dict | None:
        existing = group.get("text_layout")
        if isinstance(existing, dict):
            try:
                layout = {
                    "x": int(existing["x"]),
                    "y": int(existing["y"]),
                    "width": int(existing["width"]),
                    "height": int(existing["height"]),
                }
                if existing.get("angle") is not None:
                    layout["angle"] = float(existing["angle"])
                return layout
            except (KeyError, TypeError, ValueError):
                pass
        if image_size is not None:
            try:
                from hydra_manga_tl.phase.phase3 import placement_candidates
                candidates = placement_candidates({key: value for key, value in group.items() if key != "text_layout"}, image_size)
                if candidates:
                    return _box_text_layout(candidates[0][1])
            except (KeyError, TypeError, ValueError):
                pass
        if group.get("manual"):
            polygon = group.get("polygon", [])
            if len(polygon) == 4:
                p0, p1, p2, p3 = polygon
                dx = p1[0] - p0[0]
                dy = p1[1] - p0[1]
                angle = math.atan2(dy, dx) * 180.0 / math.pi
                if angle > 90:
                    angle -= 180
                elif angle < -90:
                    angle += 180
                width = math.hypot(dx, dy)
                height = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
                center_x = sum(p[0] for p in polygon) / 4.0
                center_y = sum(p[1] for p in polygon) / 4.0
                return {
                    "x": int(round(center_x - width / 2.0)),
                    "y": int(round(center_y - height / 2.0)),
                    "width": max(1, int(round(width))),
                    "height": max(1, int(round(height))),
                    "angle": float(round(angle, 2))
                }
        for key in ("safe_area",):
            box = group.get(key)
            if isinstance(box, list) and len(box) == 4:
                return _box_text_layout(box)
        segmentation = group.get("bubble_segmentation")
        if isinstance(segmentation, dict):
            box = segmentation.get("safe_area")
            if isinstance(box, list) and len(box) == 4:
                return _box_text_layout(box)
        box = _polygon_box(group.get("polygon", []))
        return _box_text_layout(box) if box is not None else None

    @staticmethod
    def _apply_edit_text_layout(group: dict, edit: RegionEdit) -> None:
        if all(value is not None for value in (edit.layout_x, edit.layout_y, edit.layout_width, edit.layout_height)):
            layout = {
                "x": edit.layout_x, "y": edit.layout_y,
                "width": edit.layout_width, "height": edit.layout_height,
            }
            if edit.layout_angle is not None:
                layout["angle"] = float(edit.layout_angle)
            group["text_layout"] = layout

    def _ensure_text_layouts(self, payload: dict, image_size: tuple[int, int] | None = None) -> None:
        for group in payload.get("translation_groups", []):
            if group.get("editor_replace") is False:
                continue
            layout = self._default_text_layout(group, image_size)
            if layout is not None:
                group["text_layout"] = layout

    def effective_translation_payload(self, image_index: int) -> dict:
        if self.current is None:
            raise ValueError("No project is open.")
        image = self.current.images[image_index]
        path = Path(image.translation_result)
        if path.is_file():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            payload = {
                "project_id": self.current.id,
                "source": image.source_path, "source_language": image.source_language or "",
                "target_language": self.current.target_language, "source_regions": [], "translation_groups": [],
                "literal_provider": self.current.literal_provider,
                "localization_provider": self.current.localization_provider,
                "localization_style": self.current.localization_style,
                "text_style": self.current.text_style, "bubble_padding": self.current.bubble_padding,
                "max_lines": self.current.max_lines,
            }
        payload["project_id"] = self.current.id
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
            polygon = manual.selection_polygon or manual.polygon or rect_to_polygon(manual.rect)
            placement_polygon = manual.placement_polygon or polygon
            cleanup_polygons = list(manual.cleanup_polygons) or list(manual.source_polygons) or [polygon]
            bubble_type = normalize_region_type(getattr(manual, "bubble_type", "dialogue") or "dialogue")
            render_direction = (
                "vertical-rtl" if manual.direction == "vertical-rtl" and (x2 - x1) < 40 else "horizontal-ltr"
            )
            group = {
                "index": manual.key, "manual": True, "manual_id": manual.id,
                "original_text": manual.original_text, "translated_text": normalize_global_text(manual.translated_text),
                "type": bubble_type, "bubble_type": bubble_type,
                "ocr_confidence": manual.ocr_confidence,
                "manual_rect": [x1, y1, x2, y2],
                "polygon": polygon,
                "selection_polygon": polygon,
                "cleanup_polygons": cleanup_polygons,
                "placement_polygon": placement_polygon,
                "decorative_symbols": [
                    dict(symbol) for symbol in manual.decorative_symbols
                ],
                "preserved_marks": [dict(mark) for mark in manual.preserved_marks],
                "status": manual.status, "review_reasons": list(manual.review_reasons),
                "member_region_indices": [], "direction": manual.direction,
                "source_direction": manual.direction,
                "render_direction": render_direction,
                "source_polygons": list(manual.source_polygons) or [polygon], "placement_policy": "exact",
                "source_member_texts": list(manual.source_member_texts),
                "source_language": manual.source_language,
                "source_text_hash": manual.source_text_hash,
                "source_region_hash": manual.source_region_hash,
                "translation_source": manual.translation_source,
                "provider": manual.translation_provider,
            }
            if bubble_type == "title":
                reconstruction = dict(manual.title_reconstruction)
                group.update({
                    "render_mode": manual.render_mode or "art_text",
                    "title_render_polygon": polygon,
                    "title_composition": dict(manual.title_composition),
                    "title_reconstruction": reconstruction,
                })
                reconstruction_cleanup = reconstruction.get("cleanup_polygons") or reconstruction.get("mask_polygons")
                if isinstance(reconstruction_cleanup, list) and reconstruction_cleanup:
                    group["cleanup_polygons"] = reconstruction_cleanup
                if manual.style_profile is not None:
                    group["style_profile"] = dict(manual.style_profile)
            payload["translation_groups"].append(group)
        for group in payload["translation_groups"]:
            edit = image.edits.get(str(group["index"]))
            if edit is None:
                continue
            if edit.original_text is not None:
                group["original_text"] = edit.original_text
            if edit.bubble_type is not None:
                region_type = normalize_region_type(edit.bubble_type)
                group["bubble_type"] = region_type
                group["type"] = region_type
            if edit.translated_text is not None:
                group["translated_text"] = normalize_global_text(edit.translated_text)
            if edit.style_profile is not None:
                group["style_profile"] = dict(edit.style_profile)
            group.update({
                "editor_replace": edit.replace, "font_size_override": edit.font_size,
                "placement_offset": [edit.offset_x, edit.offset_y], "font_family": edit.font_family,
                "text_color": edit.color, "alignment": edit.alignment,
            })
            self._apply_edit_text_layout(group, edit)
        image_size = None
        try:
            from PIL import Image
            with Image.open(image.source_path) as opened:
                image_size = opened.size
        except (OSError, ValueError):
            image_size = None
        self._ensure_text_layouts(payload, image_size)
        return payload

