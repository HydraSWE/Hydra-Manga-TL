"""Prototype full-page title-overlay removal and replacement.

This is intentionally kept outside the production renderer while we validate
the right behavior for cover/title text that is printed directly on art.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import os
import cv2
import numpy as np
from PIL import Image


@dataclass(frozen=True)
class Region:
    index: int
    text: str
    confidence: float
    polygon: list[list[int]]
    box: tuple[int, int, int, int]
    role: str


def _box(polygon: list[list[int]]) -> tuple[int, int, int, int]:
    xs = [int(point[0]) for point in polygon]
    ys = [int(point[1]) for point in polygon]
    return min(xs), min(ys), max(xs), max(ys)


def _jp_count(text: str) -> int:
    return sum(1 for c in text if "\u3040" <= c <= "\u30ff" or "\u3400" <= c <= "\u9fff")


def _is_title_region(region: dict, image_size: tuple[int, int]) -> bool:
    width, height = image_size
    x1, y1, x2, y2 = _box(region["polygon"])
    box_w, box_h = max(1, x2 - x1), max(1, y2 - y1)
    text = str(region.get("text", "")).strip()
    if box_h <= box_w * 1.8:
        return False
    if box_h < height * 0.22:
        return False
    if _jp_count(text) < 3:
        return False
    return x1 > width * 0.58 or x2 > width * 0.72


def _is_volume_region(region: dict, title_boxes: list[tuple[int, int, int, int]], image_size: tuple[int, int]) -> bool:
    text = str(region.get("text", "")).strip()
    if not text.isdigit() or not title_boxes:
        return False
    width, height = image_size
    x1, y1, x2, y2 = _box(region["polygon"])
    center_x = (x1 + x2) / 2
    title_left = min(box[0] for box in title_boxes)
    title_right = max(box[2] for box in title_boxes)
    return title_left - width * 0.06 <= center_x <= title_right + width * 0.06 and y1 > height * 0.55


def detect_title_regions(ocr_payload: dict, image_size: tuple[int, int]) -> list[Region]:
    raw_regions = list(ocr_payload.get("regions", []))
    title_indices = [i for i, region in enumerate(raw_regions) if _is_title_region(region, image_size)]
    title_boxes = [_box(raw_regions[i]["polygon"]) for i in title_indices]
    volume_indices = [
        i for i, region in enumerate(raw_regions)
        if i not in title_indices and _is_volume_region(region, title_boxes, image_size)
    ]
    output: list[Region] = []
    for i in title_indices + volume_indices:
        region = raw_regions[i]
        text = str(region.get("text", ""))
        role = "volume" if text.strip().isdigit() else "title"
        output.append(Region(
            index=i + 1,
            text=text,
            confidence=float(region.get("confidence", 0.0)),
            polygon=[[int(x), int(y)] for x, y in region["polygon"]],
            box=_box(region["polygon"]),
            role=role,
        ))
    return output


def build_mask(image_size: tuple[int, int], regions: list[Region], dilation: int) -> Image.Image:
    width, height = image_size
    mask = np.zeros((height, width), dtype=np.uint8)
    for region in regions:
        points = np.asarray(region.polygon, dtype=np.int32)
        cv2.fillPoly(mask, [points], 255)
    if dilation > 0:
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dilation * 2 + 1, dilation * 2 + 1))
        mask = cv2.dilate(mask, kernel)
    return Image.fromarray(mask, mode="L")


def _masked_pixels(image: Image.Image, mask: Image.Image, box: tuple[int, int, int, int]) -> np.ndarray:
    crop = np.asarray(image.crop(box).convert("RGB"))
    mask_crop = np.asarray(mask.crop(box)) > 0
    if not mask_crop.any():
        return crop.reshape(-1, 3)
    return crop[mask_crop]


def sample_title_style(image: Image.Image, mask: Image.Image, region: Region) -> dict[str, tuple[int, int, int]]:
    pixels = _masked_pixels(image, mask, region.box)
    if pixels.size == 0:
        return {"fill": (255, 255, 255), "stroke": (0, 0, 0), "accent": (120, 105, 190)}

    luminance = pixels @ np.asarray([0.2126, 0.7152, 0.0722])
    white = tuple(int(v) for v in np.percentile(pixels[luminance >= np.percentile(luminance, 92)], 75, axis=0))
    black = tuple(int(v) for v in np.percentile(pixels[luminance <= np.percentile(luminance, 8)], 25, axis=0))

    rgb = pixels.astype(np.float32)
    saturation = rgb.max(axis=1) - rgb.min(axis=1)
    colorful = pixels[(saturation >= np.percentile(saturation, 80)) & (luminance > 50) & (luminance < 235)]
    accent_values = np.median(colorful, axis=0) if len(colorful) else np.median(pixels, axis=0)
    accent = tuple(int(v) for v in accent_values)
    return {"fill": white, "stroke": black, "accent": accent}


def fit_vertical_words(
    words: list[str],
    box: tuple[int, int, int, int],
    *,
    maximum: int,
    minimum: int,
) -> tuple[float, int, int, list[tuple[int, int, int]]] | None:
    width = max(1, box[2] - box[0])
    height = max(1, box[3] - box[1])
    usable_w = int(width * 0.92)
    usable_h = int(height * 0.94)
    font_face = cv2.FONT_HERSHEY_TRIPLEX
    for size in range(maximum, minimum - 1, -1):
        scale = size / 36.0
        stroke = max(2, int(size / 9))
        metrics = []
        for word in words:
            (word_w, word_h), baseline = cv2.getTextSize(word, font_face, scale, stroke)
            metrics.append((word_w, word_h, baseline))
        total_h = sum(h + baseline for _, h, baseline in metrics) + max(0, len(words) - 1) * max(2, size // 8)
        max_w = max((w for w, _, _ in metrics), default=0)
        if max_w <= usable_w and total_h <= usable_h:
            return scale, stroke, max(2, size // 8), metrics
    return None


def draw_vertical_words(
    image: Image.Image,
    region: Region,
    text: str,
    style: dict[str, tuple[int, int, int]],
    *,
    maximum: int = 72,
) -> dict:
    words = [word.strip().upper() for word in text.split() if word.strip()]
    if not words:
        return {"region": region.index, "text": text, "rendered": False, "reason": "empty_text"}
    fitted = fit_vertical_words(words, region.box, maximum=maximum, minimum=10)
    if fitted is None:
        return {"region": region.index, "text": text, "rendered": False, "reason": "could_not_fit"}

    scale, stroke, gap, metrics = fitted
    canvas = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    font_face = cv2.FONT_HERSHEY_TRIPLEX
    x1, y1, x2, y2 = region.box
    total_h = sum(h + baseline for _, h, baseline in metrics) + gap * (len(words) - 1)
    y = y1 + max(0, (y2 - y1 - total_h) // 2)
    accent_offset = max(2, int(maximum // 18))
    positions = []
    fill_bgr = tuple(int(v) for v in reversed(style["fill"]))
    stroke_bgr = tuple(int(v) for v in reversed(style["stroke"]))
    accent_bgr = tuple(int(v) for v in reversed(style["accent"]))
    outer_bgr = fill_bgr
    outer = max(stroke + 3, 4)
    for word, (word_w, word_h, baseline) in zip(words, metrics):
        x = x1 + max(0, (x2 - x1 - word_w) // 2)
        baseline_y = y + word_h
        cv2.putText(
            canvas, word, (x + accent_offset, baseline_y + accent_offset),
            font_face, scale, outer_bgr, outer, cv2.LINE_AA,
        )
        cv2.putText(
            canvas, word, (x + accent_offset, baseline_y + accent_offset),
            font_face, scale, accent_bgr, stroke, cv2.LINE_AA,
        )
        cv2.putText(canvas, word, (x, baseline_y), font_face, scale, stroke_bgr, outer, cv2.LINE_AA)
        cv2.putText(canvas, word, (x, baseline_y), font_face, scale, fill_bgr, stroke, cv2.LINE_AA)
        positions.append([int(x), int(baseline_y - word_h)])
        y += word_h + baseline + gap
    image.paste(Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGBA)))
    return {
        "region": region.index,
        "source": region.text,
        "text": text,
        "rendered": True,
        "font_scale": scale,
        "stroke_width": stroke,
        "positions": positions,
        "sampled_style": style,
    }


def _sanitized_subprocess_env() -> dict[str, str]:
    """Return a copy of os.environ with parent PyInstaller runtime overrides removed."""
    env = os.environ.copy()
    if getattr(sys, "frozen", False):
        internal = Path(sys.executable).parent / "_internal"
        if internal.is_dir():
            internal_resolved = internal.resolve()
            paths = env.get("PATH", "").split(os.pathsep)
            cleaned_paths = [
                p for p in paths
                if p and Path(p).resolve() != internal_resolved
            ]
            env["PATH"] = os.pathsep.join(cleaned_paths)
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.pop("_MEIPASS2", None)
    return env


def run_iopaint(image: Path, mask: Path, output_dir: Path, model: str, device: str, python_executable: str) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    command = [
        python_executable, "-m", "iopaint", "run",
        "--model", model,
        "--device", device,
        "--image", str(image),
        "--mask", str(mask),
        "--output", str(output_dir),
    ]
    env = _sanitized_subprocess_env()
    completed = subprocess.run(command, check=False, env=env)
    candidates = sorted(output_dir.glob("*.png"))
    if not candidates:
        completed.check_returncode()
        raise FileNotFoundError(f"iopaint produced no PNG files in {output_dir}")
    return candidates[0]


def create_title_overlay(args: argparse.Namespace) -> dict:
    source = args.image.resolve()
    ocr_json = args.ocr_json.resolve()
    output_dir = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    original = Image.open(source).convert("RGB")
    ocr_payload = json.loads(ocr_json.read_text(encoding="utf-8"))
    title_regions = detect_title_regions(ocr_payload, original.size)
    if not title_regions:
        raise RuntimeError("No title overlay regions were detected.")

    mask = build_mask(original.size, title_regions, args.dilation)
    mask_path = output_dir / f"{source.stem}_title_mask.png"
    mask.save(mask_path)

    iopaint_dir = output_dir / "iopaint"
    if args.clean_iopaint_output and iopaint_dir.exists():
        shutil.rmtree(iopaint_dir)
    cleaned_path = run_iopaint(source, mask_path, iopaint_dir, args.model, args.device, args.iopaint_python)
    translated = Image.open(cleaned_path).convert("RGBA")

    title_text_regions = [region for region in title_regions if region.role == "title"]
    title_text_regions.sort(key=lambda region: region.box[0], reverse=True)
    volume_regions = [region for region in title_regions if region.role == "volume"]

    render_report = []
    if title_text_regions:
        main_region = title_text_regions[0]
        style = sample_title_style(original, mask, main_region)
        render_report.append(draw_vertical_words(translated, main_region, args.title_main, style, maximum=args.max_font))
    if len(title_text_regions) > 1 and args.title_sub:
        sub_region = title_text_regions[-1]
        style = sample_title_style(original, mask, sub_region)
        render_report.append(draw_vertical_words(translated, sub_region, args.title_sub, style, maximum=args.max_font))
    if volume_regions and args.volume:
        volume_region = sorted(volume_regions, key=lambda region: region.box[1])[-1]
        style = sample_title_style(original, mask, volume_region)
        render_report.append(draw_vertical_words(translated, volume_region, args.volume, style, maximum=args.max_font))

    final_path = output_dir / f"{source.stem}_title_inpainted_english.png"
    translated.convert("RGB").save(final_path)

    report = {
        "source": str(source),
        "ocr_json": str(ocr_json),
        "mask": str(mask_path),
        "iopaint_cleaned": str(cleaned_path.resolve()),
        "output": str(final_path),
        "model": args.model,
        "device": args.device,
        "dilation": args.dilation,
        "detected_title_regions": [
            {
                "index": region.index,
                "text": region.text,
                "confidence": region.confidence,
                "box": list(region.box),
                "role": region.role,
            }
            for region in title_regions
        ],
        "rendered": render_report,
    }
    report_path = output_dir / f"{source.stem}_title_inpainted_english_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["report"] = str(report_path)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Prototype title-overlay removal with LaMa/iopaint and sampled-style English lettering.")
    parser.add_argument("--image", type=Path, required=True)
    parser.add_argument("--ocr-json", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("outputs/prototype_title_inpaint"))
    parser.add_argument("--title-main", required=True, help="English text for the main detected title column.")
    parser.add_argument("--title-sub", default="", help="English text for the secondary detected title column.")
    parser.add_argument("--volume", default="", help="Volume/number text, if the detector finds a numeric title region.")
    parser.add_argument("--model", default="lama")
    parser.add_argument("--device", default="cpu")
    parser.add_argument("--iopaint-python", default=sys.executable, help="Python executable from an environment with requirements-inpaint.txt installed.")
    parser.add_argument("--dilation", type=int, default=20)
    parser.add_argument("--max-font", type=int, default=74)
    parser.add_argument("--clean-iopaint-output", action="store_true")
    args = parser.parse_args()
    report = create_title_overlay(args)
    sys.stdout.buffer.write((json.dumps(report, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
