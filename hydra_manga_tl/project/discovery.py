"""Phase 1 command line application."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw

from hydra_manga_tl.ocr.core import OCRResult, PaddleOCREngine

SUPPORTED = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff", ".bmp"}


def image_path_sort_key(path: Path) -> tuple:
    """Sort pure numeric image stems numerically without considering the extension."""
    parent = str(path.parent).casefold()
    stem = path.stem
    normalized_path = str(path).casefold()
    if stem.isascii() and stem.isdigit():
        return parent, 0, int(stem), stem.casefold(), normalized_path, str(path)
    return parent, 1, stem.casefold(), normalized_path, str(path)


def discover(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix.lower() in SUPPORTED else []
    return sorted(
        (item for item in path.rglob("*") if item.is_file() and item.suffix.lower() in SUPPORTED),
        key=image_path_sort_key,
    )


def annotate(source: Path, destination: Path, result: OCRResult) -> None:
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    draw = ImageDraw.Draw(image)
    for index, region in enumerate(result.regions, 1):
        points = [tuple(point) for point in region.polygon]
        draw.line(points + [points[0]], fill=(255, 40, 40), width=max(2, image.width // 400))
        anchor = min(points, key=lambda point: (point[1], point[0]))
        draw.text(anchor, str(index), fill=(30, 80, 255), stroke_width=2, stroke_fill="white")
    image.save(destination, format="PNG")


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def run(inputs: Iterable[Path], output: Path, languages: list[str]) -> list[dict[str, object]]:
    files = sorted(
        {file.resolve() for item in inputs for file in discover(item)},
        key=image_path_sort_key,
    )
    if not files:
        raise ValueError("No supported images were found.")
    output.mkdir(parents=True, exist_ok=True)
    engine = PaddleOCREngine(languages)
    summaries: list[dict[str, object]] = []
    for index, image_path in enumerate(files, 1):
        print(f"[{index}/{len(files)}] Analyzing {image_path.name} ...", flush=True)
        result = engine.analyze(image_path)
        stem = image_path.stem
        write_json(output / f"{stem}.json", result.to_dict())
        annotate(image_path, output / f"{stem}_annotated.png", result)
        summary = {
            "file": image_path.name,
            "language": result.language,
            "language_confidence": round(result.language_confidence, 4),
            "ocr_confidence": round(result.average_ocr_confidence, 4),
            "text_regions": len(result.regions),
            "model_language": result.model_language,
        }
        summaries.append(summary)
        print(f"  {result.language}: {len(result.regions)} regions, OCR {result.average_ocr_confidence:.1%}")
    write_json(output / "report.json", {"images": summaries, "count": len(summaries)})
    return summaries


def main() -> int:
    parser = argparse.ArgumentParser(description="Detect manga text and source language.")
    parser.add_argument("inputs", nargs="+", type=Path, help="Image files or folders")
    parser.add_argument("--output", type=Path, default=Path("outputs/phase1"))
    parser.add_argument("--languages", default="ch,japan,en", help="Comma-separated PaddleOCR model languages")
    args = parser.parse_args()
    try:
        run(args.inputs, args.output, [item.strip() for item in args.languages.split(",") if item.strip()])
    except (ValueError, FileNotFoundError) as error:
        parser.error(str(error))
    return 0
