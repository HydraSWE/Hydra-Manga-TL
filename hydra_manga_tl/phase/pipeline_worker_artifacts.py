"""PipelineWorker payload, timing, and debug artifact helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from hydra_manga_tl import __version__
from hydra_manga_tl.ocr.core import OCRResult
from hydra_manga_tl.phase.layout import TextGroup
from hydra_manga_tl.phase.pipeline_helpers import _box_from_polygon, _write_json_atomic
from hydra_manga_tl.phase.segmentation import segment_bubble
from hydra_manga_tl.translation.engines import PageDialogue, PageTranslation
from hydra_manga_tl.translation.memory import learn_validated_page


class PipelineWorkerArtifactsMixin:
    """Build translation payloads and persist pipeline timing/debug artifacts."""
    def _build_translation_payload(
        self,
        *,
        source: Path,
        source_language: str,
        ocr_result: OCRResult,
        source_regions: list[dict[str, Any]],
        translated_groups: list[dict[str, Any]],
        preprocessed: Any,
        layout_graph: dict[str, Any],
        bubble_segmentations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "pipeline_version": __version__,
            "project_id": str(self.config.get("project_id") or ""),
            "source": str(source.resolve()),
            "source_language": source_language,
            "target_language": self.target,
            "ocr_model_language": ocr_result.model_language,
            "source_regions": source_regions,
            "translation_groups": translated_groups,
            "preprocessing": (
                preprocessed.to_dict() if hasattr(preprocessed, "to_dict")
                else dict(preprocessed)
            ),
            "ocr_attempts": ocr_result.metadata.get("manager", {}),
            "layout_graph": layout_graph,
            "bubble_segmentation": bubble_segmentations,
            "translation_units": layout_graph["translation_units"],
            "literal_provider": self.config.get("literal_provider", "marian"),
            "localization_provider": self.config.get("localization_provider", "local"),
            "localization_style": self.config.get("localization_style", "Manga"),
            "text_style": self.config.get("text_style", "Manga"),
            "bubble_padding": int(self.config.get("bubble_padding", 5)),
            "max_lines": int(self.config.get("max_lines", 3)),
        }

    def _learn_translation_groups(
        self,
        page: PageDialogue,
        page_result: PageTranslation,
        translated_groups: list[dict[str, Any]],
    ) -> int:
        if not (
            self.config.get("translation_memory_enabled", True)
            and self.config.get("translation_memory_auto_learn", True)
        ):
            return 0
        resolved_by_id = {
            str(item.get("id", "")): dict(item)
            for item in page_result.translations
        }
        valid_ids: list[str] = []
        final_translations: list[dict[str, Any]] = []
        for group in translated_groups:
            entry_id = f"r{int(group.get('index', 0) or 0)}"
            if entry_id not in resolved_by_id:
                continue
            detail = resolved_by_id[entry_id]
            detail["text"] = str(group.get("translated_text", ""))
            final_translations.append(detail)
            if (
                group.get("status") == "translated"
                and not group.get("review_reasons")
                and str(group.get("translated_text", "")).strip()
            ):
                valid_ids.append(entry_id)
        if not valid_ids:
            return 0
        return learn_validated_page(
            page,
            PageTranslation(
                page.source_language,
                page.target_language,
                final_translations,
            ),
            valid_ids=valid_ids,
            project_id=str(self.config.get("project_id") or "") or None,
        )

    def _write_image_timing(self, image_id: str, timing: dict[str, Any]) -> None:
        path = self.target_artifacts / f"{image_id}_timing.json"
        _write_json_atomic(path, timing)

    def _segment_groups(
        self, source: Path, image_id: str, groups: list[TextGroup], group_payloads: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        mask_dir = self.artifacts / image_id / "bubble_masks"
        with Image.open(source) as opened:
            original_page = opened.convert("RGB")
        results: list[dict[str, Any]] = []
        for index, (group, payload) in enumerate(zip(groups, group_payloads), 1):
            polygon = payload.get("polygon", [
                [group.bbox[0], group.bbox[1]], [group.bbox[2], group.bbox[1]],
                [group.bbox[2], group.bbox[3]], [group.bbox[0], group.bbox[3]],
            ])
            results.append(segment_bubble(
                original_page,
                polygon,
                bubble_type=str(payload.get("type", "speech")),
                padding=int(self.config.get("bubble_padding", 5)),
                mask_path=mask_dir / f"group_{index:03d}.png",
            ).to_dict())
        return results

    def _write_batch_timing(self, image_timings: list[dict[str, Any]], elapsed_seconds: float) -> None:
        summary = {
            "total_seconds": round(elapsed_seconds, 3),
            "image_count": len(image_timings),
            "ocr_cache_hits": sum(1 for item in image_timings if item.get("cache", {}).get("ocr_hit")),
            "translation_cache_hits": sum(1 for item in image_timings if item.get("cache", {}).get("translation_hit")),
            "images": image_timings,
        }
        path = self.target_artifacts / "pipeline_timing_summary.json"
        _write_json_atomic(path, summary)

    def _write_ocr_debug_artifacts(
        self, source: Path, image_id: str, source_regions: list[dict], groups: list[TextGroup], layout_graph: dict[str, Any],
    ) -> dict[str, str]:
        debug_dir = self.artifacts / image_id / "ocr_debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(source) as opened:
            image = opened.convert("RGB")
        overlay = image.copy()
        draw = ImageDraw.Draw(overlay)
        for index, region in enumerate(source_regions, 1):
            polygon = [[int(point[0]), int(point[1])] for point in region.get("polygon", [])]
            if len(polygon) >= 2:
                draw.line(polygon + [polygon[0]], fill=(220, 40, 40), width=2)
                x1, y1, _, _ = _box_from_polygon(polygon)
                draw.text((x1, max(0, y1 - 12)), f"r{index}", fill=(220, 40, 40))
        group_debug = []
        for index, group in enumerate(groups, 1):
            x1, y1, x2, y2 = [int(value) for value in group.bbox]
            draw.rectangle([x1, y1, x2, y2], outline=(40, 110, 230), width=3)
            draw.text((x1, y1), f"g{index}", fill=(40, 110, 230))
            pad = 8
            crop_box = (
                max(0, x1 - pad),
                max(0, y1 - pad),
                min(image.width, x2 + pad),
                min(image.height, y2 + pad),
            )
            crop_path = debug_dir / f"group_{index:03d}.png"
            if crop_box[2] > crop_box[0] and crop_box[3] > crop_box[1]:
                image.crop(crop_box).save(crop_path)
            group_debug.append({
                "index": index,
                "bbox": [x1, y1, x2, y2],
                "direction": group.direction,
                "text": group.text,
                "member_region_indices": group.member_indices,
                "crop": str(crop_path),
            })
        ocr_overlay = debug_dir / f"{source.stem}_ocr_overlay.png"
        overlay.save(ocr_overlay)
        reading_overlay = image.copy()
        reading_draw = ImageDraw.Draw(reading_overlay)
        nodes_by_id = {
            str(node.get("id")): node
            for node in layout_graph.get("nodes", [])
            if isinstance(node, dict)
        }
        for order, node_id in enumerate(layout_graph.get("reading_order", []), 1):
            node = nodes_by_id.get(str(node_id))
            if not node:
                continue
            x1, y1, x2, y2 = [int(value) for value in node.get("bbox", [0, 0, 0, 0])]
            reading_draw.rectangle([x1, y1, x2, y2], outline=(25, 160, 80), width=3)
            reading_draw.text((x1, max(0, y1 - 14)), str(order), fill=(25, 160, 80))
        reading_path = debug_dir / f"{source.stem}_reading_order_overlay.png"
        reading_overlay.save(reading_path)

        bubble_overlay = image.copy()
        bubble_draw = ImageDraw.Draw(bubble_overlay, "RGBA")
        safe_overlay = image.copy()
        safe_draw = ImageDraw.Draw(safe_overlay, "RGBA")
        for group in groups:
            x1, y1, x2, y2 = [int(value) for value in group.bbox]
            bubble_draw.rectangle([x1, y1, x2, y2], outline=(80, 150, 255, 220), width=3)
            bubble_draw.rectangle([x1, y1, x2, y2], fill=(80, 150, 255, 28))
            inset = max(3, int(min(max(1, x2 - x1), max(1, y2 - y1)) * 0.08))
            safe_draw.rectangle([x1 + inset, y1 + inset, x2 - inset, y2 - inset], outline=(255, 180, 40, 230), width=3)
            safe_draw.rectangle([x1 + inset, y1 + inset, x2 - inset, y2 - inset], fill=(255, 180, 40, 28))
        bubble_path = debug_dir / f"{source.stem}_bubble_mask_overlay.png"
        safe_path = debug_dir / f"{source.stem}_safe_area_overlay.png"
        bubble_overlay.save(bubble_path)
        safe_overlay.save(safe_path)
        (debug_dir / "groups.json").write_text(json.dumps(group_debug, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "ocr_overlay": str(ocr_overlay.resolve()),
            "bubble_mask_overlay": str(bubble_path.resolve()),
            "safe_area_overlay": str(safe_path.resolve()),
            "reading_order_overlay": str(reading_path.resolve()),
            "groups": str((debug_dir / "groups.json").resolve()),
        }


