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

from .title import (
    TitleObject,
    TitleRenderSettings,
    composite_title,
    extract_title_style,
    render_title,
)
from .title.style_profile import FillProfile, OutlineProfile, TitleStyleProfile


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
        return group.get("art_text") is True or group.get("render_mode") == "art_text"

    group_type = str(group.get("type") or group.get("bubble_type") or "").strip().lower()
    if group_type in {"dialogue", "speech", "narration"}:
        return False
    if group_type and group_type not in {"sfx", "sound_effect", "credit", "decorative", "title", "sign"}:
        return False

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
    title = TitleObject(
        id="sample",
        polygon=polygon,
        original_text="",
        translated_text="",
        render_settings=TitleRenderSettings(),
        metadata={"renderable_type": "title", "render_mode": "art_text"},
    )
    profile = extract_title_style(original, mask, title)
    fill = (profile.fill.dominant_color if profile.fill else None) or (255, 255, 255)
    stroke = (profile.outline.color if profile.outline else None) or (0, 0, 0)
    accent = (
        profile.glow.color if profile.glow and profile.glow.color else
        profile.shadow.color if profile.shadow and profile.shadow.color else
        fill
    )
    return {"fill": fill, "stroke": stroke, "accent": accent}


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
    rendered = []
    for polygon, chunk in zip(polygons, chunks):
        if not str(chunk).strip():
            continue
        title = TitleObject(
            id=str(group.get("title_id") or group.get("index") or "art"),
            polygon=[[int(x), int(y)] for x, y in polygon],
            original_text=str(group.get("original_text") or ""),
            translated_text=str(chunk).upper(),
            style_profile=TitleStyleProfile.from_dict(group.get("style_profile") if isinstance(group.get("style_profile"), dict) else None),
            render_settings=TitleRenderSettings(
                max_font_size=int(group.get("art_text_max_font", maximum) or maximum),
                min_font_size=10,
                alignment=str(group.get("alignment", "center")),
            ),
            metadata={"renderable_type": "title", "render_mode": "art_text"},
        )
        profile = title.style_profile
        if profile.fill is None or profile.outline is None:
            extracted = extract_title_style(original, mask, title)
            profile = TitleStyleProfile(
                fill=profile.fill or extracted.fill or FillProfile(),
                outline=profile.outline or extracted.outline or OutlineProfile(),
                shadow=profile.shadow or extracted.shadow,
                glow=profile.glow or extracted.glow,
                gradient=profile.gradient or extracted.gradient,
                typography=profile.typography or extracted.typography,
                rotation=profile.rotation if profile.rotation is not None else extracted.rotation,
                opacity=profile.opacity if profile.opacity is not None else extracted.opacity,
                blend_mode=profile.blend_mode or extracted.blend_mode,
                metadata={**extracted.metadata, **profile.metadata},
            )
        layer = render_title(image, title, profile)
        composited = composite_title(image, layer, title.render_settings)
        image.paste(composited)
        report = layer.report()
        style = sample_style(original, mask, polygon)
        rendered.append({
            "polygon": polygon,
            "text": chunk,
            "positions": report["positions"],
            "sampled_style": style,
            "style_profile": report["style_profile"],
            "font_size": report["font_size"],
            "overflow": report["overflow"],
        })
    return {"text": group.get("translated_text", ""), "art_runs": rendered}
