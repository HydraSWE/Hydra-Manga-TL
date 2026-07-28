"""Phase 3: remove source text and render translations in the same locations."""

from __future__ import annotations

import argparse
import gc
import json
import logging
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

from hydra_manga_tl.phase.art_inpaint import inpaint_python, is_art_text_group, render_art_text
from hydra_manga_tl.core.normalization import normalize_global_text
from hydra_manga_tl.core.region_types import group_region_type, is_title_like_region
from hydra_manga_tl.phase.segmentation import reusable_segmentation
from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.title import extract_title_glyph_mask
from hydra_manga_tl.title.reconstruction import reconstruct_title_page
# Updated imports to match the new layout engine in renderer.py
from hydra_manga_tl.phase.renderer import (
    clean_background, 
    compute_safe_text_area, 
    detect_bubble_box, 
    expanded_box, 
    fit_text, 
    make_mask, 
    render_group
)

DEFAULT_FONT = Path(r"C:\Windows\Fonts\arial.ttf")
LOGGER = logging.getLogger(__name__)
FONT_PATHS = {
    "Arial": Path(r"C:\Windows\Fonts\arial.ttf"),
    "Arial Bold": Path(r"C:\Windows\Fonts\arialbd.ttf"),
    "Comic Sans MS": Path(r"C:\Windows\Fonts\comic.ttf"),
    "Segoe UI": Path(r"C:\Windows\Fonts\segoeui.ttf"),
}
STYLE_DEFAULTS = {
    "Manga": {"font_family": "Arial Bold", "alignment": "center", "max_lines": 3},
    "Comic": {"font_family": "Comic Sans MS", "alignment": "center", "max_lines": 3},
    "Novel": {"font_family": "Segoe UI", "alignment": "left", "max_lines": 5},
}

PREVIEW_MAX_SIDE = 1800


def _preview_pair(original: Image.Image, final: Image.Image) -> Image.Image:
    """Build a bounded side-by-side preview without duplicating full pages."""
    left = ImageOps.contain(original, (PREVIEW_MAX_SIDE // 2, PREVIEW_MAX_SIDE), Image.Resampling.LANCZOS)
    right = ImageOps.contain(final, (PREVIEW_MAX_SIDE // 2, PREVIEW_MAX_SIDE), Image.Resampling.LANCZOS)
    height = max(left.height, right.height)
    preview = Image.new("RGB", (left.width + right.width, height), "white")
    preview.paste(left, (0, (height - left.height) // 2))
    preview.paste(right, (left.width, (height - right.height) // 2))
    return preview


def resolve_font_path(font_family: str, fallback: Path = DEFAULT_FONT) -> Path:
    """Resolve an editor font choice to a renderable Windows font file."""
    path = FONT_PATHS.get(font_family, fallback)
    return path if path.is_file() else fallback


def decorative_horizontal(group: dict, size: tuple[int, int]) -> bool:
    """Keep banners, credits, logos, and horizontal SFX out of dialogue rendering."""
    if group.get("render_direction", group.get("direction")) != "horizontal-ltr":
        return False
    x1, y1, x2, y2 = group["polygon"][0][0], group["polygon"][0][1], group["polygon"][2][0], group["polygon"][2][1]
    width, height = max(1, x2 - x1), max(1, y2 - y1)
    image_width, image_height = size
    return width / height >= 2.7 or (width >= image_width * 0.20 and y1 <= image_height * 0.25)


def inputs(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if "_translated_" in path.stem else []
    return sorted(path.glob("*_translated_*.json"))


def should_replace(group: dict, source_language: str, policy: str) -> tuple[bool, str | None]:
    if source_language == "Latin-script" or group["status"] == "preserved":
        return False, "preserved"
    if (
        group_region_type(group) == "title"
        and group.get("render_mode") == "art_text"
        and group.get("manual")
    ):
        return True, None
    if not group.get("translated_text") or "translation_unchanged" in group.get("review_reasons", []):
        return False, "no_reliable_translation"
    if group.get("status") == "review" and "low_confidence_or_empty" in group.get("review_reasons", []):
        return False, "low_confidence_or_empty"
    if str(group.get("translated_text", "")).strip() == str(group.get("original_text", "")).strip():
        return False, "translation_unchanged"
    if policy == "safe" and group["status"] != "translated":
        return False, group["status"]
    if policy == "safe" and decorative_horizontal(group, (10_000, 10_000)):
        return False, "decorative_or_title"
    return True, None


def renderer_for_region(group: dict) -> str:
    """Return the renderer family for a region payload."""
    if is_title_like_region(group):
        return "title"
    if is_art_text_group(group, (10_000, 10_000)):
        return "title"
    return "dialogue"


def is_title_renderer_region(group: dict) -> bool:
    return renderer_for_region(group) == "title"


def _region_layout_polygon(group: dict, image_size: tuple[int, int]) -> list[list[int]] | None:
    box = _text_layout_box(group, image_size)
    if box is None:
        return None
    x1, y1, x2, y2 = box
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def _title_render_group(group: dict, image_size: tuple[int, int]) -> dict:
    """Build the renderer input contract for title-like regions."""
    routed = dict(group)
    region_type = group_region_type(routed)
    routed["type"] = region_type
    routed["bubble_type"] = region_type
    routed["renderable_type"] = region_type
    routed["render_mode"] = "art_text"
    layout_polygon = _region_layout_polygon(routed, image_size)
    if layout_polygon is not None:
        routed["title_render_polygon"] = layout_polygon
        routed["placement_policy"] = "text_layout"
    return routed


def _clip_box(box: list[int], image_size: tuple[int, int]) -> list[int]:
    return [
        max(0, int(box[0])), max(0, int(box[1])),
        min(image_size[0], int(box[2])), min(image_size[1], int(box[3])),
    ]


def _offset_box(box: list[int], offset: list[int], image_size: tuple[int, int]) -> list[int]:
    offset_x, offset_y = offset
    return _clip_box([box[0] + offset_x, box[1] + offset_y, box[2] + offset_x, box[3] + offset_y], image_size)


def _same_box(first: list[int], second: list[int]) -> bool:
    return all(abs(int(a) - int(b)) <= 1 for a, b in zip(first, second))


def _box_from_polygon(polygon: list[list[int]]) -> list[int]:
    xs = [int(point[0]) for point in polygon]
    ys = [int(point[1]) for point in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def _box_area(box: list[int]) -> int:
    return max(0, int(box[2]) - int(box[0])) * max(0, int(box[3]) - int(box[1]))


def _intersection_area(first: list[int], second: list[int]) -> int:
    x1 = max(int(first[0]), int(second[0]))
    y1 = max(int(first[1]), int(second[1]))
    x2 = min(int(first[2]), int(second[2]))
    y2 = min(int(first[3]), int(second[3]))
    return max(0, x2 - x1) * max(0, y2 - y1)


def _center_distance_ratio(first: list[int], second: list[int]) -> float:
    first_w = max(1, int(first[2]) - int(first[0]))
    first_h = max(1, int(first[3]) - int(first[1]))
    first_cx = (int(first[0]) + int(first[2])) / 2.0
    first_cy = (int(first[1]) + int(first[3])) / 2.0
    second_cx = (int(second[0]) + int(second[2])) / 2.0
    second_cy = (int(second[1]) + int(second[3])) / 2.0
    return (((second_cx - first_cx) / first_w) ** 2 + ((second_cy - first_cy) / first_h) ** 2) ** 0.5


def _validated_segmentation_box(group: dict, segmentation: dict, image_size: tuple[int, int]) -> list[int] | None:
    safe_area = _clip_box([int(value) for value in segmentation["safe_area"]], image_size)
    source_box = _clip_box(_box_from_polygon(group["polygon"]), image_size)
    source_area = max(1, _box_area(source_box))
    safe_area_size = _box_area(safe_area)
    if safe_area_size <= 0:
        return None
    overlap = _intersection_area(source_box, safe_area) / source_area
    distance = _center_distance_ratio(source_box, safe_area)
    if overlap < 0.18 and distance > 0.65:
        return None
    source_w = max(1, source_box[2] - source_box[0])
    source_h = max(1, source_box[3] - source_box[1])
    safe_w = max(1, safe_area[2] - safe_area[0])
    safe_h = max(1, safe_area[3] - safe_area[1])
    if safe_w < min(42, source_w * 0.55) and safe_h < min(72, source_h * 0.45):
        return None
    return safe_area


def _manual_exact_box(group: dict, image_size: tuple[int, int]) -> list[int] | None:
    if group.get("placement_policy") != "exact" and not group.get("manual"):
        return None
    rect = group.get("manual_rect")
    if isinstance(rect, list) and len(rect) == 4:
        return _clip_box([int(value) for value in rect], image_size)
    polygon = group.get("polygon") or []
    if polygon:
        return _clip_box(_box_from_polygon(polygon), image_size)
    return None


def _text_layout_box(group: dict, image_size: tuple[int, int]) -> list[int] | None:
    layout = group.get("text_layout")
    if not isinstance(layout, dict):
        return None
    try:
        x = int(layout["x"])
        y = int(layout["y"])
        width = int(layout["width"])
        height = int(layout["height"])
    except (KeyError, TypeError, ValueError):
        return None
    if width <= 0 or height <= 0:
        return None
    return _clip_box([x, y, x + width, y + height], image_size)


def _title_glyph_mask(image: Image.Image, groups: list[dict], image_size: tuple[int, int]) -> tuple[np.ndarray, list[dict]]:
    combined = np.zeros((image_size[1], image_size[0]), dtype=np.uint8)
    reports: list[dict] = []
    for group in groups:
        result = extract_title_glyph_mask(image, group, image_size)
        report = result.report()
        report["group"] = group.get("index")
        group["title_mask_report"] = report
        reports.append(report)
        if result.accepted:
            combined = np.maximum(combined, result.mask)
    return combined, reports


def _vertical_source_cluster_box(group: dict, image_size: tuple[int, int]) -> list[int] | None:
    """Use the main vertical OCR column cluster when a stray SFX column was grouped."""
    if group.get("source_direction", group.get("direction")) != "vertical-rtl":
        return None
    polygons = [polygon for polygon in group.get("source_polygons", []) if polygon]
    if len(polygons) < 2:
        return None
    boxes = [_box_from_polygon(polygon) for polygon in polygons]
    centers = [((box[0] + box[2]) / 2.0, box) for box in boxes]
    widths = [max(1, box[2] - box[0]) for box in boxes]
    median_width = sorted(widths)[len(widths) // 2]
    tolerance = max(70, median_width * 1.8)
    best_cluster: list[list[int]] = []
    for center_x, _ in centers:
        cluster = [box for other_x, box in centers if abs(other_x - center_x) <= tolerance]
        if len(cluster) > len(best_cluster):
            best_cluster = cluster
    if len(best_cluster) < 2 or len(best_cluster) == len(boxes):
        return None
    x1 = min(box[0] for box in best_cluster)
    y1 = min(box[1] for box in best_cluster)
    x2 = max(box[2] for box in best_cluster)
    y2 = max(box[3] for box in best_cluster)
    return expanded_box([[x1, y1], [x2, y1], [x2, y2], [x1, y2]], image_size)


def placement_candidates(group: dict, image_size: tuple[int, int], image: Image.Image | None = None) -> list[tuple[str, list[int]]]:
    """Return anchored-first placement boxes, falling back to detected bubbles."""
    polygon = group["polygon"]
    offset = group.get("placement_offset", [0, 0])
    candidates: list[tuple[str, list[int]]] = []

    def add(name: str, box: list[int] | None) -> None:
        if box is None:
            return
        clipped = _offset_box(box, offset, image_size)
        if clipped[2] <= clipped[0] or clipped[3] <= clipped[1]:
            return
        if any(_same_box(clipped, existing) for _, existing in candidates):
            return
        candidates.append((name, clipped))

    layout_box = _text_layout_box(group, image_size)
    if layout_box is not None:
        candidates.append(("text_layout", layout_box))
        return candidates

    exact_box = _manual_exact_box(group, image_size)
    if exact_box is not None:
        add("manual_exact_bounds", exact_box)
        return candidates

    segmentation = reusable_segmentation(group.get("bubble_segmentation"), image_size)
    if segmentation is not None:
        add("segmented_safe_area", _validated_segmentation_box(group, segmentation, image_size))
    add("source_cluster_bounds", _vertical_source_cluster_box(group, image_size))
    if segmentation is None:
        add("segmented_safe_area", group.get("safe_area"))
    add("anchored_text_bounds", expanded_box(polygon, image_size))

    # A validated phase-2 segmentation is authoritative. Avoid repeating
    # connected-component bubble detection for each render candidate.
    if segmentation is None and group.get("placement_policy") != "exact" and image is not None:
        padding = int(group.get("bubble_padding", 5))
        add("detected_bubble", detect_bubble_box(image, polygon, padding))
        add("safe_bubble_area", compute_safe_text_area(image, polygon, padding))

    return candidates


def placement_box(group: dict, image_size: tuple[int, int], image: Image.Image | None = None) -> list[int]:
    """Return the preferred placement box for legacy callers."""
    return placement_candidates(group, image_size, image)[0][1]


def prepare_group_fit(
    group: dict, image_size: tuple[int, int], fallback_font: Path = DEFAULT_FONT,
    image: Image.Image | None = None,
    *,
    strict_font_override: bool = True,
):
    """Resolve a group's font and fit, rejecting invalid fixed sizes explicitly."""
    defaults = STYLE_DEFAULTS.get(group.get("text_style", "Manga"), STYLE_DEFAULTS["Manga"])
    group_font = resolve_font_path(group.get("font_family", defaults["font_family"]), fallback_font)
    text_layout_box = _text_layout_box(group, image_size)
    orig_polygon = group["polygon"]
    if text_layout_box is not None:
        layout_h = max(1, text_layout_box[3] - text_layout_box[1])
        layout_w = max(1, text_layout_box[2] - text_layout_box[0])
        dynamic_max = min(160, max(72, int(min(layout_h, layout_w) * 1.15)))
        maximum = dynamic_max
    else:
        orig_h = max(1, max(p[1] for p in orig_polygon) - min(p[1] for p in orig_polygon))
        dynamic_max = min(72, int(orig_h * 1.5))
        if group.get("placement_policy") == "exact" or group.get("manual"):
            dynamic_max = min(72, max(dynamic_max, int(orig_h * 0.32)))
        if group.get("source_direction", group.get("direction")) == "vertical-rtl":
            dynamic_max = min(dynamic_max, 40 if (group.get("placement_policy") == "exact" or group.get("manual")) else 32)
        maximum = 28 if decorative_horizontal(group, image_size) else dynamic_max
    max_lines = int(group.get("max_lines", defaults["max_lines"]) or 0)
    if (group.get("placement_policy") == "exact" or group.get("manual")) and max_lines <= defaults["max_lines"]:
        box = text_layout_box if text_layout_box is not None else _manual_exact_box(group, image_size)
        if box is not None:
            box_height = max(1, box[3] - box[1])
            max_lines = max(max_lines, min(8, max(3, box_height // 42)))

    override = int(group.get("font_size_override", 0) or 0)
    auto_minimum = int(group.get("minimum_font_size") or 0)
    if auto_minimum <= 0:
        exact_or_manual = group.get("placement_policy") == "exact" or group.get("manual")
        vertical_source = group.get("source_direction", group.get("direction")) == "vertical-rtl"
        auto_minimum = 5 if text_layout_box is not None else (10 if (vertical_source and not exact_or_manual) else 5)
    largest = None
    for strategy, box in placement_candidates(group, image_size, image):
        if override > 0:
            fitted = fit_text(group["translated_text"], box, group_font, maximum=override, minimum=override, max_lines=max_lines)
            if fitted is None:
                fallback = fit_text(group["translated_text"], box, group_font, maximum=max(auto_minimum, override - 1), minimum=auto_minimum, max_lines=max_lines)
                if fallback is not None and (largest is None or fallback.font_size > largest.font_size):
                    setattr(fallback, "placement_strategy", strategy)
                    largest = fallback
                continue
        else:
            fitted = fit_text(group["translated_text"], box, group_font, maximum=maximum, minimum=auto_minimum, max_lines=max_lines)
            if fitted is None:
                continue
        setattr(fitted, "placement_strategy", strategy)
        return fitted, group_font

    if override > 0:
        if not strict_font_override and largest is not None:
            setattr(largest, "placement_strategy", getattr(largest, "placement_strategy", "font_override_clamped"))
            setattr(largest, "requested_font_size", override)
            return largest, group_font
        suggestion = f"; the largest fitting size is {largest.font_size}" if largest is not None else ""
        raise ValueError(f"Font size {override} does not fit text block {group['index']}{suggestion}.")
    return None, group_font

def run(input_path: Path, output: Path, font_path: Path = DEFAULT_FONT, policy: str = "complete") -> dict:
    files = inputs(input_path)
    if not files:
        raise ValueError("No Phase 2 translated JSON files were found.")
    if not font_path.is_file():
        raise FileNotFoundError(f"Font not found: {font_path}")
    output.mkdir(parents=True, exist_ok=True)
    reports = []
    for number, path in enumerate(files, 1):
        gc.collect()
        payload = json.loads(path.read_text(encoding="utf-8"))
        for group in payload.get("translation_groups", []):
            if "translated_text" in group:
                group["translated_text"] = normalize_global_text(group.get("translated_text", ""))
        source = Path(payload["source"])
        print(f"[{number}/{len(files)}] Reconstructing {source.name} ...", flush=True)
        with Image.open(source) as opened:
            original = opened.convert("RGB")
        size = original.size
        stem = source.stem
        eligible, render_jobs, title_jobs, skipped = [], [], [], []
        for group in payload["translation_groups"]:
            region_type = group_region_type(group)
            group["bubble_type"] = region_type
            group["type"] = region_type
            group.setdefault("text_style", payload.get("text_style", "Manga"))
            group.setdefault("bubble_padding", payload.get("bubble_padding", 5))
            group.setdefault("max_lines", payload.get("max_lines", STYLE_DEFAULTS.get(group["text_style"], STYLE_DEFAULTS["Manga"])["max_lines"]))
            style_defaults = STYLE_DEFAULTS.get(group["text_style"], STYLE_DEFAULTS["Manga"])
            group.setdefault("alignment", style_defaults["alignment"])
            if group.get("editor_replace") is False:
                skipped.append({"group": group["index"], "reason": "disabled_in_editor"})
                continue
            replace, reason = should_replace(group, group.get("source_language", payload["source_language"]), policy)
            if replace and policy == "safe" and decorative_horizontal(group, size):
                replace, reason = False, "decorative_or_title"
            if not replace:
                skipped.append({"group": group["index"], "reason": reason})
                continue
            if is_title_renderer_region(group):
                title_group = _title_render_group(group, size)
                eligible.append(title_group)
                title_jobs.append(title_group)
                continue
            # Fix 3: Preprocess vertical-rtl text before fit_text so line calculation
            # matches what will be rendered (each word becomes a separate line).
            direction = group.get("render_direction", group.get("direction", "horizontal-ltr"))
            if direction == "vertical-rtl":
                group["translated_text"] = group["translated_text"].replace(" ", "\n")
            fitted, group_font = prepare_group_fit(group, size, font_path, original, strict_font_override=False)
            if fitted is None:
                skipped.append({"group": group["index"], "reason": "text_does_not_fit"})
                continue
            eligible.append(group)
            render_jobs.append((group, fitted, group_font))

        normal_groups = [group for group in eligible if group not in title_jobs]
        title_mask_reports: list[dict] = []
        title_reconstruction_reports: list[dict] = []
        if title_jobs:
            normal_mask = make_mask(size, normal_groups, dilation=2) if normal_groups else np.zeros((size[1], size[0]), dtype=np.uint8)
            reconstruction = reconstruct_title_page(
                original,
                title_jobs,
                normal_mask,
                output_dir=output / "_inpaint_work",
                stem=stem,
                provider_id=str(payload.get("title_reconstruction_provider") or SETTINGS.title_reconstruction_provider or "opencv"),
                analysis_provider_id=str(payload.get("reconstruction_analysis_provider") or SETTINGS.reconstruction_analysis_provider or "none"),
                inpaint_python_resolver=inpaint_python,
                configured_inpaint_python=payload.get("inpaint_python"),
            )
            mask = reconstruction.mask
            cleaned = reconstruction.image
            cleaning_method = reconstruction.cleaning_method
            inpaint_warning = reconstruction.inpaint_warning
            title_mask_reports = reconstruction.title_mask_reports
            title_reconstruction_reports = reconstruction.reports
            background_plate_reports = reconstruction.background_plate_reports
        else:
            mask = make_mask(size, eligible)
            cleaning_method = "unchanged"
            inpaint_warning = ""
            background_plate_reports: list[dict] = []
            cleaned = None
        mask_image = Image.fromarray(mask)
        if cleaned is None:
            source_array = np.asarray(original)
            cleaned_array, base_method = clean_background(source_array, mask)
            cleaned = Image.fromarray(cleaned_array)
            del source_array, cleaned_array
            if cleaning_method == "unchanged":
                cleaning_method = base_method
        cleaned_path = output / f"{stem}_cleaned.png"
        final_path = output / f"{stem}_translated_en.png"
        mask_path = output / f"{stem}_mask.png"
        preview_path = output / f"{stem}_preview.png"
        cleaned.save(cleaned_path)
        final = cleaned
        rendered = []
        for group, fitted, group_font in render_jobs:
            safe_text = str(group["translated_text"]).encode("ascii", "backslashreplace").decode("ascii")
            strategy = getattr(fitted, "placement_strategy", "unknown")
            message = (
                f"RENDER: group={group['index']} strategy={strategy} "
                f"font={fitted.font_size} box={fitted.box} direction={group.get('direction')} text={safe_text}"
            )
            print(message, flush=True)
            LOGGER.info(message)
            
            details = render_group(
                final,
                fitted,
                group["translated_text"],
                group_font,
                color=group.get("text_color"),
                alignment=group.get("alignment", "center"),
                direction=group.get("render_direction", group.get("direction", "horizontal-ltr")),
            )
            details["group"] = group["index"]
            details["source_region_indices"] = group.get("member_region_indices", [])
            details["placement_strategy"] = strategy
            details["renderer"] = "dialogue"
            details["region_type"] = group_region_type(group)
            rendered.append(details)
        for group in title_jobs:
            details = render_art_text(
                final,
                original,
                mask_image,
                group,
                project_id=str(payload.get("project_id") or ""),
                maximum=int(group.get("art_text_max_font", 74) or 74),
            )
            details["group"] = group["index"]
            details["source_region_indices"] = group.get("member_region_indices", [])
            details["placement_strategy"] = "title_renderer"
            details["renderer"] = "title"
            details["region_type"] = group_region_type(group)
            if isinstance(group.get("title_mask_report"), dict):
                details.update(group["title_mask_report"])
            if isinstance(group.get("title_background_plate_report"), dict):
                details.update(group["title_background_plate_report"])
            if isinstance(group.get("title_reconstruction_report"), dict):
                details.update(group["title_reconstruction_report"])
            rendered.append(details)

        final.save(final_path)
        mask_image.save(mask_path)
        preview = _preview_pair(original, final)
        preview.save(preview_path)
        preview.close()
        image_report = {
            "source": str(source.resolve()), "output": final_path.name,
            "cleaning_method": cleaning_method, "rendered_groups": rendered,
            "rendered_count": len(rendered), "skipped_groups": skipped,
            # Updated placement policy label
            "placement_policy": "smart safe area layout",
            "replacement_policy": policy,
        }
        if title_mask_reports:
            accepted_count = sum(1 for item in title_mask_reports if not item.get("needs_title_mask_review"))
            image_report["title_mask_reports"] = title_mask_reports
            image_report["title_mask_method"] = "glyph"
            image_report["title_mask_accepted_count"] = accepted_count
            image_report["needs_title_mask_review"] = accepted_count < len(title_mask_reports)
        if title_reconstruction_reports:
            image_report["title_reconstruction_reports"] = title_reconstruction_reports
            image_report["title_reconstruction_version"] = title_reconstruction_reports[0].get("title_reconstruction_version")
            image_report["title_reconstruction_provider"] = title_reconstruction_reports[0].get("title_reconstruction_provider")
            image_report["reconstruction_analysis_provider"] = title_reconstruction_reports[0].get("reconstruction_analysis_provider")
            image_report["title_reconstruction_accepted_count"] = sum(
                1 for item in title_reconstruction_reports if item.get("title_reconstruction_status") == "accepted"
            )
            image_report["needs_title_mask_review"] = any(
                item.get("title_reconstruction_status") != "accepted" for item in title_reconstruction_reports
            )
        if background_plate_reports:
            plate_count = sum(1 for item in background_plate_reports if not item.get("title_background_plate_warning"))
            image_report["title_background_plate_reports"] = background_plate_reports
            image_report["title_background_plate_accepted_count"] = plate_count
            if plate_count:
                image_report["needs_title_mask_review"] = False
        if inpaint_warning:
            image_report["inpaint_warning"] = inpaint_warning
        (output / f"{stem}_render.json").write_text(json.dumps(image_report, ensure_ascii=False, indent=2), encoding="utf-8")
        reports.append({"file": source.name, "rendered": len(rendered), "skipped": len(skipped), "output": final_path.name})
        print(f"  rendered {len(rendered)} in-place; skipped {len(skipped)}")
        original.close()
        final.close()
        mask_image.close()
        gc.collect()
    report = {"count": len(reports), "replacement_policy": policy, "images": reports}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Replace original image text with Phase 2 translations.")
    parser.add_argument("input", type=Path, help="Phase 2 output folder or translated JSON")
    parser.add_argument("--output", type=Path, default=Path("outputs/phase3"))
    parser.add_argument("--font", type=Path, default=DEFAULT_FONT)
    parser.add_argument("--policy", choices=["complete", "safe"], default="complete",
                        help="complete replaces review/title text; safe keeps it unchanged")
    args = parser.parse_args()
    try:
        run(args.input, args.output, args.font, args.policy)
    except (ValueError, FileNotFoundError, json.JSONDecodeError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
