"""Fast pipeline page preparation for PipelineWorker."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any
import time

from PIL import Image

from hydra_manga_tl import __version__
from hydra_manga_tl.core.language import resolve_source_language
from hydra_manga_tl.core.normalization import normalize_global_text
from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.ocr.service import OCRService
from hydra_manga_tl.phase.job_manifest import JobManifest
from hydra_manga_tl.phase.layout import TextGroup, classify_text_group, group_regions
from hydra_manga_tl.phase.layout_graph import build_layout_graph
from hydra_manga_tl.phase.pipeline_helpers import (
    _auto_translate_region_type,
    _bubble_display_id,
    _cache_fragment,
    _classify_bubble,
    _ocr_cache_key,
    _ocr_settings_fingerprint,
    _preprocessing_settings_fingerprint,
    _preprocessing_stage_fingerprint,
    _source_text_color,
    _stable_bubble_id,
    _stable_json_hash,
    _state_manager_for_stages,
    _text_group_without_preserved_marks,
    _write_json_atomic,
)
from hydra_manga_tl.phase.preprocessor import prepare_ocr_image
from hydra_manga_tl.phase.state_manager import StageAction, StageContract, StageValidationRequest
from hydra_manga_tl.title import detect_title_objects
from hydra_manga_tl.translation.memory import source_region_hash, source_text_hash
from hydra_manga_tl.translation.queue import RequestCancelled


class PipelineWorkerFastPrepMixin:
    """Prepare OCR, layout, and dialogue payloads for fast pipeline pages."""
    def _prepare_fast_page(
        self,
        item: dict[str, Any],
        position: int,
        total: int,
        ocr_service: OCRService,
        requested_source: str,
        preferred: str | None,
        job_manifest: JobManifest,
    ) -> dict[str, Any]:
        image_id, source = str(item["id"]), Path(item["source_path"])
        image_started = time.perf_counter()
        timing: dict[str, Any] = {
            "image_id": image_id,
            "source": str(source),
            "position": position,
            "total": total,
            "source_language_request": requested_source,
            "quality": "Fast",
            "stages": {},
            "counts": {},
            "cache": {"ocr_hit": False, "translation_hit": False},
        }
        job_manifest.ensure_page(image_id, str(source))
        job_manifest.mark(image_id, "preprocessing")
        self.stage.emit(image_id, "preprocessing", position, total, f"Preparing {source.name}")
        started = time.perf_counter()
        ocr_path = self.artifacts / f"{image_id}_ocr.json"
        preprocessed = prepare_ocr_image(source, self.artifacts / image_id / "preprocess")
        timing["stages"]["preprocess_seconds"] = round(time.perf_counter() - started, 3)
        preprocessing_settings = _preprocessing_settings_fingerprint(
            self.config,
            "Fast",
        )
        _record_stage_completion(job_manifest,
            image_id,
            "preprocessing",
            input_fingerprint=_preprocessing_stage_fingerprint(
                source,
                preprocessing_settings,
            ),
            input_artifacts={"source_image": source},
            artifacts={
                "preprocessed_image": Path(preprocessed.ocr_path),
            },
            source_path=source,
            application_version=__version__,
            settings_fingerprint=preprocessing_settings,
            metadata={"quality": "Fast"},
        )
        if self.cancel.is_set():
            raise RequestCancelled("Request cancelled")

        started = time.perf_counter()
        ocr_cache_key = _stable_json_hash({
            "kind": "ocr-v3-preprocessed",
            "base": _ocr_cache_key(source, requested_source, "Fast", preferred),
            "preprocessing": preprocessed.quality.to_dict(),
        })
        paths = self.paths()
        ocr_cache_path = paths.ocr_cache / f"{_cache_fragment(source.stem)}_{ocr_cache_key[:16]}.json"
        ocr_settings = _ocr_settings_fingerprint(
            requested_source,
            "Fast",
            preferred,
            preprocessed.quality.to_dict(),
        )
        ocr_plan = _state_manager_for_stages(
            job_manifest,
            StageContract("OCR"),
        ).plan_page(
            image_id,
            source,
            {
                "OCR": StageValidationRequest(
                    input_fingerprint=ocr_cache_key,
                    artifacts={"ocr_result": ocr_path},
                    input_artifacts={
                        "preprocessed_image": Path(preprocessed.ocr_path),
                    },
                    source_path=source,
                    application_version=__version__,
                    settings_fingerprint=ocr_settings,
                ),
            },
        )
        force_retranslate = bool(self.config.get("force_retranslate", False))
        verified_checkpoint = (
            ocr_plan.action_for("OCR") is StageAction.SKIP
            and not force_retranslate
        )
        if not verified_checkpoint:
            job_manifest.mark(image_id, "OCR", stage="preprocessing")
            self.stage.emit(
                image_id,
                "ocr",
                position,
                total,
                f"Reading text in {source.name}",
            )
        service_result = self._run_ocr_page(
            ocr_service,
            Path(preprocessed.ocr_path),
            preferred_language=preferred,
            quality="Fast",
            auto_language_fallback=False,
            cache_path=None if force_retranslate else ocr_cache_path,
            checkpoint_path=ocr_path if verified_checkpoint else None,
        )
        ocr_result = service_result.ocr_result
        source_regions = service_result.final_regions
        source_language = resolve_source_language(requested_source, ocr_result.language)
        timing["cache"]["ocr_hit"] = service_result.cache_hit
        timing["cache"]["ocr_checkpoint_verified"] = verified_checkpoint
        timing["ocr_service"] = service_result.telemetry
        timing["stages"]["ocr_seconds"] = round(time.perf_counter() - started, 3)
        _write_json_atomic(ocr_path, ocr_result.to_dict())
        _record_stage_completion(job_manifest,
            image_id,
            "OCR",
            input_fingerprint=ocr_cache_key,
            artifacts={"ocr_result": ocr_path},
            input_artifacts={
                "preprocessed_image": Path(preprocessed.ocr_path),
            },
            source_path=source,
            application_version=__version__,
            settings_fingerprint=ocr_settings,
            metadata={
                "quality": "Fast",
                "source_language_request": requested_source,
                "preferred_language": preferred or "",
            },
        )

        with Image.open(source) as opened:
            page_size = opened.size
        layout_graph = build_layout_graph(source_regions, page_size=page_size)
        groups = group_regions(source_regions)
        with Image.open(source) as opened:
            with opened.convert("RGB") as source_image:
                group_visuals = []
                for group in groups:
                    effective_group, text_member_indices, decorative_symbols = _text_group_without_preserved_marks(
                        group,
                        source_regions,
                    )
                    polygons = [
                        source_regions[index - 1]["polygon"]
                        for index in text_member_indices
                    ]
                    cleanup_polygons = [
                        *polygons,
                        *[
                            symbol.get("polygon")
                            for symbol in decorative_symbols
                            if isinstance(symbol, dict) and symbol.get("polygon")
                        ],
                    ]
                    group_visuals.append((
                        effective_group,
                        polygons,
                        cleanup_polygons,
                        [
                            _source_text_color(source_image, polygon)
                            for polygon in polygons
                        ],
                        source_region_hash(source_image, polygons),
                        decorative_symbols,
                    ))
        group_payloads: list[dict[str, Any]] = []
        group_source_polygons: list[list[list[list[int]]]] = []
        dialogue: list[dict[str, Any]] = []
        for idx, group in enumerate(groups):
            stable_bubble_id = _stable_bubble_id(
                str(self.config.get("project_id") or ""),
                image_id,
                idx,
            )
            display_bubble_id = _bubble_display_id(position, idx)
            placement_bbox = group.bbox
            effective_group, polygons, cleanup_polygons, source_text_colors, region_hash, decorative_symbols = group_visuals[idx]
            placement_bbox = effective_group.bbox
            confidence = min(source_regions[index - 1]["confidence"] for index in effective_group.member_indices)
            bubble_type = _classify_bubble(effective_group)
            classification = classify_text_group(effective_group, source_regions, ocr_result.model_language)
            if bubble_type not in {"credit", "sfx"}:
                bubble_type = classification.kind
            candidate = {
                "type": bubble_type,
                "text": effective_group.text,
                "original_text": effective_group.text,
                "polygon": [
                    [placement_bbox[0], placement_bbox[1]], [placement_bbox[2], placement_bbox[1]],
                    [placement_bbox[2], placement_bbox[3]], [placement_bbox[0], placement_bbox[3]],
                ],
                "source_direction": group.direction,
                "source_polygons": polygons,
                "cleanup_polygons": cleanup_polygons,
                "source_text_colors": source_text_colors,
                "decorative_symbols": decorative_symbols,
                "preserved_marks": [],
            }
            if bubble_type not in {"credit", "sfx", "sign"} and detect_title_objects([candidate], page_size):
                bubble_type = "title"
            payload = {
                "type": bubble_type,
                "text": effective_group.text,
                "confidence": confidence,
                "classification_reasons": classification.reasons,
                "ocr_review_reasons": list(dict.fromkeys(
                    str(reason)
                    for member_index in group.member_indices
                    for reason in source_regions[member_index - 1].get("ocr_review_reasons", [])
                    if str(reason)
                )),
                "polygon": candidate["polygon"],
                "source_text_colors": source_text_colors,
                "cleanup_polygons": cleanup_polygons,
                "decorative_symbols": decorative_symbols,
                "preserved_marks": [],
                "source_text_hash": source_text_hash(effective_group.text),
                "source_region_hash": region_hash,
                "bubble_id": stable_bubble_id,
                "display_id": display_bubble_id,
            }
            group_payloads.append(payload)
            group_source_polygons.append(polygons)
            region_type = normalize_region_type(bubble_type)
            if _auto_translate_region_type(region_type, self.config):
                dialogue.append({
                    "id": f"r{idx + 1}",
                    "bubble_id": stable_bubble_id,
                    "display_id": display_bubble_id,
                    "text": effective_group.text,
                    "decorative_symbols": decorative_symbols,
                    "preserved_marks": [],
                    "confidence": confidence,
                    "source_text": str(effective_group.text).strip(),
                    "reading_order": len(dialogue) + 1,
                    "source_direction": group.direction,
                    "bbox": payload["polygon"],
                    "region_type": region_type,
                    "source_text_hash": payload["source_text_hash"],
                    "source_region_hash": payload["source_region_hash"],
                })
        timing["counts"] = {
            "source_regions": len(source_regions),
            "groups": len(groups),
            "layout_edges": len(layout_graph.edges),
        }
        debug_artifacts = (
            self._write_ocr_debug_artifacts(source, image_id, source_regions, groups, layout_graph.to_dict())
            if bool(self.config.get("debug_artifacts_enabled", False))
            else {}
        )
        bubble_segmentations = self._segment_groups(source, image_id, groups, group_payloads)
        prepared = {
            "item": item,
            "position": position,
            "source": str(source),
            "source_language": source_language,
            "ocr_path": str(ocr_path),
            "ocr_result": ocr_result.to_dict(),
            "source_regions": source_regions,
            "preprocessed": preprocessed.to_dict(),
            "page_size": list(page_size),
            "layout_graph": layout_graph.to_dict(),
            "groups": [asdict(item[0]) for item in group_visuals],
            "group_payloads": group_payloads,
            "group_source_polygons": group_source_polygons,
            "bubble_segmentations": bubble_segmentations,
            "dialogue": dialogue,
            "debug_artifacts": debug_artifacts,
            "timing": timing,
            "image_started": image_started,
        }
        prepared_path = self.target_artifacts / "fast_jobs" / f"{position - 1:06d}_{image_id}.json"
        _write_json_atomic(prepared_path, prepared)
        return {"path": str(prepared_path), "dialogue": dialogue, "position": position, "image_id": image_id}


