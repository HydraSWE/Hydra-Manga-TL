"""Sequential OCR grouping helpers for PipelineWorker."""

from __future__ import annotations

from PIL import Image

from hydra_manga_tl.phase.layout import classify_text_group, group_regions
from hydra_manga_tl.phase.layout_graph import build_layout_graph
from hydra_manga_tl.phase.pipeline_helpers import (
    _classify_bubble,
    _source_text_color,
    _text_group_without_preserved_marks,
)
from hydra_manga_tl.title import detect_title_objects
from hydra_manga_tl.translation.memory import source_region_hash, source_text_hash


class PipelineWorkerSequentialGroupingMixin:
    def _build_sequential_groups(
        self,
        *,
        source,
        image_id,
        source_regions,
        ocr_result,
        timing,
    ):
        with Image.open(source) as opened:
            source_image = opened.convert("RGB")
            page_size = source_image.size
        layout_graph = build_layout_graph(source_regions, page_size=page_size)
        groups = group_regions(source_regions)
        group_payloads = []
        group_source_polygons = []
        effective_groups = []
        
        for group in groups:
            effective_group, text_member_indices, decorative_symbols = _text_group_without_preserved_marks(
                group,
                source_regions,
            )
            effective_groups.append(effective_group)
            text = effective_group.text
            placement_bbox = effective_group.bbox
            confidence = min(source_regions[index - 1]["confidence"] for index in effective_group.member_indices)
            polygons = [source_regions[index - 1]["polygon"] for index in text_member_indices]
            cleanup_polygons = [
                *polygons,
                *[
                    symbol.get("polygon")
                    for symbol in decorative_symbols
                    if isinstance(symbol, dict) and symbol.get("polygon")
                ],
            ]
            source_text_colors = [_source_text_color(source_image, polygon) for polygon in polygons]

            bubble_type = _classify_bubble(effective_group)
            classification = classify_text_group(effective_group, source_regions, ocr_result.model_language)
            if bubble_type not in {"credit", "sfx"}:
                bubble_type = classification.kind
            candidate_payload = {
                "type": bubble_type,
                "text": text,
                "original_text": text,
                "polygon": [[placement_bbox[0], placement_bbox[1]], [placement_bbox[2], placement_bbox[1]], [placement_bbox[2], placement_bbox[3]], [placement_bbox[0], placement_bbox[3]]],
                "source_direction": group.direction,
                "source_polygons": polygons,
                "cleanup_polygons": cleanup_polygons,
                "source_text_colors": source_text_colors,
                "decorative_symbols": decorative_symbols,
                "preserved_marks": [],
            }
            if bubble_type not in {"credit", "sfx", "sign"} and detect_title_objects([candidate_payload], page_size):
                bubble_type = "title"

            group_payloads.append({
                "type": bubble_type,
                "text": text,
                "confidence": confidence,
                "classification_reasons": classification.reasons,
                "ocr_review_reasons": list(dict.fromkeys(
                    str(reason)
                    for member_index in group.member_indices
                    for reason in source_regions[member_index - 1].get("ocr_review_reasons", [])
                    if str(reason)
                )),
                "polygon": [[placement_bbox[0], placement_bbox[1]], [placement_bbox[2], placement_bbox[1]], [placement_bbox[2], placement_bbox[3]], [placement_bbox[0], placement_bbox[3]]],
                "source_text_hash": source_text_hash(text),
                "source_region_hash": source_region_hash(
                    source_image,
                    polygons,
                ),
                "cleanup_polygons": cleanup_polygons,
                "decorative_symbols": decorative_symbols,
                "preserved_marks": [],
            })
            group_source_polygons.append(polygons)

        debug_artifacts = (
            self._write_ocr_debug_artifacts(source, image_id, source_regions, effective_groups, layout_graph.to_dict())
            if bool(self.config.get("debug_artifacts_enabled", False))
            else {}
        )
        timing["counts"]["source_regions"] = len(source_regions)
        timing["counts"]["groups"] = len(groups)
        timing["counts"]["layout_edges"] = len(layout_graph.edges)
        return {
            "source_image": source_image,
            "page_size": page_size,
            "layout_graph": layout_graph,
            "groups": groups,
            "effective_groups": effective_groups,
            "group_payloads": group_payloads,
            "group_source_polygons": group_source_polygons,
            "debug_artifacts": debug_artifacts,
        }
