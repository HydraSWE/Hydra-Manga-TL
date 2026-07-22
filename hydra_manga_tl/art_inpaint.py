"""Optional title/text-on-art inpainting and sampled-style rendering."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np
from PIL import Image


class InpaintRuntimeUnavailable(RuntimeError):
    """Raised when the optional LaMa/iopaint helper runtime is not installed."""


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def inpaint_runtime_candidates() -> list[Path]:
    candidates: list[Path] = []
    configured = os.environ.get("HYDRA_INPAINT_PYTHON", "").strip()
    if configured:
        candidates.append(Path(configured))
    base = app_base_dir()
    candidates.append(base / "runtime" / "inpaint" / "hydra-inpaint.exe")
    candidates.append(base / "runtime" / "inpaint" / "hydra-inpaint" / "hydra-inpaint.exe")
    candidates.append(base / "runtime" / "inpaint" / "python.exe")
    candidates.append(base / "runtime" / "inpaint" / "Scripts" / "python.exe")
    bundle_value = getattr(sys, "_MEIPASS", None)
    if bundle_value:
        bundle_root = Path(bundle_value)
        candidates.append(bundle_root / "runtime" / "inpaint" / "hydra-inpaint.exe")
        candidates.append(bundle_root / "runtime" / "inpaint" / "hydra-inpaint" / "hydra-inpaint.exe")
        candidates.append(bundle_root / "runtime" / "inpaint" / "python.exe")
        candidates.append(bundle_root / "runtime" / "inpaint" / "Scripts" / "python.exe")
    if not getattr(sys, "frozen", False):
        candidates.append(base / ".venv-inpaint" / "Scripts" / "python.exe")
    deduped: list[Path] = []
    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key not in seen:
            deduped.append(candidate)
            seen.add(key)
    return deduped


def _box(polygon: list[list[int]]) -> list[int]:
    xs = [int(point[0]) for point in polygon]
    ys = [int(point[1]) for point in polygon]
    return [min(xs), min(ys), max(xs), max(ys)]


def _box_size(box: list[int]) -> tuple[int, int]:
    return max(1, box[2] - box[0]), max(1, box[3] - box[1])


def inpaint_python() -> str:
    for candidate in inpaint_runtime_candidates():
        if candidate.is_file():
            return str(candidate)
    expected = ", ".join(str(path) for path in inpaint_runtime_candidates())
    raise InpaintRuntimeUnavailable(f"LaMa inpaint runtime is unavailable. Expected one of: {expected}")


def _iopaint_command(executable: str, image_path: Path, mask_path: Path, output_dir: Path, model: str, device: str) -> list[str]:
    base = [
        "run",
        "--model", model,
        "--device", device,
        "--image", str(image_path),
        "--mask", str(mask_path),
        "--output", str(output_dir),
    ]
    if Path(executable).name.lower() == "hydra-inpaint.exe":
        return [executable, *base]
    return [executable, "-m", "iopaint", *base]


def is_art_text_group(group: dict, image_size: tuple[int, int]) -> bool:
    """Detect text printed directly on art, rather than normal speech bubbles."""
    if group.get("art_text") is True or group.get("render_mode") == "art_text":
        return True
    if group.get("manual"):
        return group.get("placement_policy") == "exact"

    width, height = image_size
    polygon = group.get("polygon") or []
    if not polygon:
        return False
    x1, y1, x2, y2 = _box(polygon)
    box_w, box_h = _box_size([x1, y1, x2, y2])
    direction = group.get("source_direction", group.get("direction", ""))
    is_vertical = direction == "vertical-rtl" or box_h > box_w * 1.65
    near_cover_edge = x1 > width * 0.55 or x2 > width * 0.72
    tall_title = box_h > height * 0.24
    multi_column = len(group.get("source_polygons", [])) >= 2 and box_h > height * 0.22
    return bool(is_vertical and near_cover_edge and (tall_title or multi_column))


def run_lama_inpaint(
    image_path: Path,
    mask_path: Path,
    output_dir: Path,
    *,
    python_executable: str | None = None,
    model: str = "lama",
    device: str = "cpu",
) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    executable = python_executable or inpaint_python()
    if not Path(executable).is_file():
        raise InpaintRuntimeUnavailable(f"LaMa inpaint runtime is unavailable: {executable}")
    command = _iopaint_command(executable, image_path, mask_path, output_dir, model, device)
    try:
        completed = subprocess.run(command, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError as exc:
        raise InpaintRuntimeUnavailable(f"LaMa inpaint runtime is unavailable: {executable}") from exc
    candidates = sorted(output_dir.glob("*.png"))
    if candidates:
        return candidates[0]
    detail = (completed.stderr or completed.stdout or "").strip().splitlines()
    message = detail[-1] if detail else f"iopaint exited with {completed.returncode}"
    completed.check_returncode()
    raise RuntimeError(message)


def clean_art_text_background(
    original: Image.Image,
    mask: Image.Image,
    work_dir: Path,
    stem: str,
    *,
    python_executable: str | None = None,
) -> tuple[Image.Image, str]:
    work_dir.mkdir(parents=True, exist_ok=True)
    source_path = work_dir / f"{stem}_art_source.png"
    mask_path = work_dir / f"{stem}_art_mask.png"
    output_dir = work_dir / f"{stem}_lama"
    if output_dir.exists():
        shutil.rmtree(output_dir)
    original.convert("RGB").save(source_path)
    mask.convert("L").save(mask_path)
    cleaned_path = run_lama_inpaint(source_path, mask_path, output_dir, python_executable=python_executable)
    return Image.open(cleaned_path).convert("RGB"), "lama-iopaint"


def sample_style(original: Image.Image, mask: Image.Image, polygon: list[list[int]]) -> dict[str, tuple[int, int, int]]:
    box = _box(polygon)
    crop = np.asarray(original.crop(tuple(box)).convert("RGB"))
    mask_crop = np.asarray(mask.crop(tuple(box)).convert("L")) > 0
    pixels = crop[mask_crop] if mask_crop.any() else crop.reshape(-1, 3)
    if pixels.size == 0:
        return {"fill": (255, 255, 255), "stroke": (0, 0, 0), "accent": (120, 105, 190)}
    luminance = pixels @ np.asarray([0.2126, 0.7152, 0.0722])
    bright = pixels[luminance >= np.percentile(luminance, 90)]
    dark = pixels[luminance <= np.percentile(luminance, 10)]
    white = tuple(int(v) for v in np.percentile(bright if len(bright) else pixels, 75, axis=0))
    black = tuple(int(v) for v in np.percentile(dark if len(dark) else pixels, 25, axis=0))
    rgb = pixels.astype(np.float32)
    saturation = rgb.max(axis=1) - rgb.min(axis=1)
    colorful = pixels[(saturation >= np.percentile(saturation, 78)) & (luminance > 45) & (luminance < 235)]
    accent_values = np.median(colorful, axis=0) if len(colorful) else np.median(pixels, axis=0)
    accent = tuple(int(v) for v in accent_values)
    return {"fill": white, "stroke": black, "accent": accent}


def split_translation_for_polygons(text: str, source_texts: list[str], count: int) -> list[str]:
    words = [word for word in str(text).replace("\n", " ").split(" ") if word]
    if count <= 1 or len(words) <= 1:
        return [str(text)] if count else []
    if len(source_texts) != count:
        source_texts = ["x"] * count
    weights = [max(1, len(str(value).strip())) for value in source_texts]
    total = sum(weights)
    remaining_words = len(words)
    remaining_weight = total
    chunks: list[str] = []
    start = 0
    for index, weight in enumerate(weights):
        if index == count - 1:
            take = remaining_words
        else:
            take = max(1, round(remaining_words * weight / max(1, remaining_weight)))
            take = min(take, remaining_words - (count - index - 1))
        chunks.append(" ".join(words[start:start + take]))
        start += take
        remaining_words -= take
        remaining_weight -= weight
    return chunks


def _fit_words(words: list[str], box: list[int], maximum: int = 74) -> tuple[float, int, int, list[tuple[int, int, int]]] | None:
    box_w, box_h = _box_size(box)
    usable_w, usable_h = int(box_w * 0.92), int(box_h * 0.94)
    font_face = cv2.FONT_HERSHEY_TRIPLEX
    for size in range(maximum, 9, -1):
        scale = size / 36.0
        stroke = max(2, int(size / 9))
        metrics = []
        for word in words:
            (word_w, word_h), baseline = cv2.getTextSize(word, font_face, scale, stroke)
            metrics.append((word_w, word_h, baseline))
        gap = max(2, size // 8)
        total_h = sum(h + baseline for _, h, baseline in metrics) + max(0, len(words) - 1) * gap
        max_w = max((w for w, _, _ in metrics), default=0)
        if max_w <= usable_w and total_h <= usable_h:
            return scale, stroke, gap, metrics
    return None


def render_art_text(
    image: Image.Image,
    original: Image.Image,
    mask: Image.Image,
    group: dict,
    *,
    maximum: int = 74,
) -> dict:
    polygons = group.get("source_polygons") or [group.get("polygon", [])]
    polygons = [polygon for polygon in polygons if polygon]
    source_texts = group.get("source_member_texts") or []
    chunks = split_translation_for_polygons(group.get("translated_text", ""), source_texts, len(polygons))
    canvas = cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)
    rendered = []
    for polygon, chunk in zip(polygons, chunks):
        words = [word.strip().upper() for word in chunk.split() if word.strip()]
        if not words:
            continue
        box = _box(polygon)
        fitted = _fit_words(words, box, maximum=maximum)
        if fitted is None:
            continue
        scale, stroke, gap, metrics = fitted
        style = sample_style(original, mask, polygon)
        fill_bgr = tuple(int(v) for v in reversed(style["fill"]))
        stroke_bgr = tuple(int(v) for v in reversed(style["stroke"]))
        accent_bgr = tuple(int(v) for v in reversed(style["accent"]))
        outer = max(stroke + 3, 4)
        accent_offset = max(2, int(maximum // 18))
        x1, y1, x2, y2 = box
        total_h = sum(h + baseline for _, h, baseline in metrics) + gap * (len(words) - 1)
        y = y1 + max(0, (y2 - y1 - total_h) // 2)
        positions = []
        for word, (word_w, word_h, baseline) in zip(words, metrics):
            x = x1 + max(0, (x2 - x1 - word_w) // 2)
            baseline_y = y + word_h
            cv2.putText(canvas, word, (x + accent_offset, baseline_y + accent_offset), cv2.FONT_HERSHEY_TRIPLEX, scale, fill_bgr, outer, cv2.LINE_AA)
            cv2.putText(canvas, word, (x + accent_offset, baseline_y + accent_offset), cv2.FONT_HERSHEY_TRIPLEX, scale, accent_bgr, stroke, cv2.LINE_AA)
            cv2.putText(canvas, word, (x, baseline_y), cv2.FONT_HERSHEY_TRIPLEX, scale, stroke_bgr, outer, cv2.LINE_AA)
            cv2.putText(canvas, word, (x, baseline_y), cv2.FONT_HERSHEY_TRIPLEX, scale, fill_bgr, stroke, cv2.LINE_AA)
            positions.append([int(x), int(baseline_y - word_h)])
            y += word_h + baseline + gap
        rendered.append({"polygon": polygon, "text": chunk, "positions": positions, "sampled_style": style})
    image.paste(Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)))
    return {"text": group.get("translated_text", ""), "art_runs": rendered}
