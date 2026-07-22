"""Phase 3: remove source text and render translations in the same locations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from PIL import Image

from .art_inpaint import InpaintRuntimeUnavailable, clean_art_text_background, inpaint_python, is_art_text_group, render_art_text
# Updated imports to match the new layout engine in renderer.py
from .renderer import (
    clean_background, 
    compute_safe_text_area, 
    detect_bubble_box, 
    expanded_box, 
    fit_text, 
    make_mask, 
    render_group
)

DEFAULT_FONT = Path(r"C:\Windows\Fonts\arial.ttf")
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

    add("anchored_text_bounds", expanded_box(polygon, image_size))

    if group.get("placement_policy") != "exact" and image is not None:
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
):
    """Resolve a group's font and fit, rejecting invalid fixed sizes explicitly."""
    defaults = STYLE_DEFAULTS.get(group.get("text_style", "Manga"), STYLE_DEFAULTS["Manga"])
    group_font = resolve_font_path(group.get("font_family", defaults["font_family"]), fallback_font)
    orig_polygon = group["polygon"]
    orig_h = max(1, max(p[1] for p in orig_polygon) - min(p[1] for p in orig_polygon))
    dynamic_max = min(72, int(orig_h * 1.5))
    if group.get("source_direction", group.get("direction")) == "vertical-rtl":
        dynamic_max = min(dynamic_max, 32)
    maximum = 28 if decorative_horizontal(group, image_size) else dynamic_max
    max_lines = int(group.get("max_lines", defaults["max_lines"]) or 0)

    override = int(group.get("font_size_override", 0) or 0)
    largest = None
    for strategy, box in placement_candidates(group, image_size, image):
        if override > 0:
            fitted = fit_text(group["translated_text"], box, group_font, maximum=override, minimum=override, max_lines=max_lines)
            if fitted is None:
                fallback = fit_text(group["translated_text"], box, group_font, maximum=max(5, override - 1), minimum=5, max_lines=max_lines)
                if fallback is not None and (largest is None or fallback.font_size > largest.font_size):
                    largest = fallback
                continue
        else:
            fitted = fit_text(group["translated_text"], box, group_font, maximum=maximum, max_lines=max_lines)
            if fitted is None:
                continue
        setattr(fitted, "placement_strategy", strategy)
        return fitted, group_font

    if override > 0:
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
        payload = json.loads(path.read_text(encoding="utf-8"))
        source = Path(payload["source"])
        print(f"[{number}/{len(files)}] Reconstructing {source.name} ...", flush=True)
        with Image.open(source) as opened:
            original = opened.convert("RGB")
        size = original.size
        stem = source.stem
        eligible, render_jobs, art_jobs, skipped = [], [], [], []
        for group in payload["translation_groups"]:
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
            if is_art_text_group(group, size):
                eligible.append(group)
                art_jobs.append(group)
                continue
            # Fix 3: Preprocess vertical-rtl text before fit_text so line calculation
            # matches what will be rendered (each word becomes a separate line).
            direction = group.get("render_direction", group.get("direction", "horizontal-ltr"))
            if direction == "vertical-rtl":
                group["translated_text"] = group["translated_text"].replace(" ", "\n")
            fitted, group_font = prepare_group_fit(group, size, font_path, original)
            if fitted is None:
                skipped.append({"group": group["index"], "reason": "text_does_not_fit"})
                continue
            eligible.append(group)
            render_jobs.append((group, fitted, group_font))

        normal_groups = [group for group in eligible if group not in art_jobs]
        if art_jobs:
            normal_mask = make_mask(size, normal_groups, dilation=2) if normal_groups else np.zeros((size[1], size[0]), dtype=np.uint8)
            art_mask = make_mask(size, art_jobs, dilation=int(payload.get("art_text_mask_dilation", 20)))
            mask = np.maximum(normal_mask, art_mask)
        else:
            mask = make_mask(size, eligible)
        source_array = np.asarray(original)
        cleaning_method = "unchanged"
        inpaint_warning = ""
        cleaned = None
        if art_jobs:
            try:
                configured_inpaint_python = payload.get("inpaint_python")
                cleaned, cleaning_method = clean_art_text_background(
                    original, Image.fromarray(mask), output / "_inpaint_work", stem,
                    python_executable=configured_inpaint_python or inpaint_python(),
                )
            except InpaintRuntimeUnavailable as error:
                cleaned = None
                inpaint_warning = str(error)
                cleaning_method = "opencv-inpaint-fallback:inpaint_runtime_unavailable"
            except Exception as error:
                cleaned = None
                inpaint_warning = str(error) or type(error).__name__
                cleaning_method = f"opencv-inpaint-fallback:{type(error).__name__}"
        if cleaned is None:
            cleaned_array, base_method = clean_background(source_array, mask)
            cleaned = Image.fromarray(cleaned_array)
            if cleaning_method == "unchanged":
                cleaning_method = base_method
        final = cleaned.copy()
        rendered = []
        for group, fitted, group_font in render_jobs:
            safe_text = str(group["translated_text"]).encode("ascii", "backslashreplace").decode("ascii")
            print("RENDER:", safe_text, group.get("direction"))
            
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
            details["placement_strategy"] = getattr(fitted, "placement_strategy", "unknown")
            rendered.append(details)
        for group in art_jobs:
            details = render_art_text(
                final,
                original,
                Image.fromarray(mask),
                group,
                maximum=int(group.get("art_text_max_font", 74) or 74),
            )
            details["group"] = group["index"]
            details["source_region_indices"] = group.get("member_region_indices", [])
            details["placement_strategy"] = "art_text_columns"
            rendered.append(details)

        cleaned_path = output / f"{stem}_cleaned.png"
        final_path = output / f"{stem}_translated_en.png"
        mask_path = output / f"{stem}_mask.png"
        preview_path = output / f"{stem}_preview.png"
        cleaned.save(cleaned_path)
        final.save(final_path)
        Image.fromarray(mask).save(mask_path)
        preview = Image.new("RGB", (size[0] * 2, size[1]), "white")
        preview.paste(original, (0, 0))
        preview.paste(final, (size[0], 0))
        preview.save(preview_path)
        image_report = {
            "source": str(source.resolve()), "output": final_path.name,
            "cleaning_method": cleaning_method, "rendered_groups": rendered,
            "rendered_count": len(rendered), "skipped_groups": skipped,
            # Updated placement policy label
            "placement_policy": "smart safe area layout",
            "replacement_policy": policy,
        }
        if inpaint_warning:
            image_report["inpaint_warning"] = inpaint_warning
        (output / f"{stem}_render.json").write_text(json.dumps(image_report, ensure_ascii=False, indent=2), encoding="utf-8")
        reports.append({"file": source.name, "rendered": len(rendered), "skipped": len(skipped), "output": final_path.name})
        print(f"  rendered {len(rendered)} in-place; skipped {len(skipped)}")
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
