"""PaddleOCR adapter and model-selection policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import tempfile
import threading
from typing import Any, ClassVar

from PIL import Image, ImageOps

from .language import detect_language, script_fit


FULL_PAGE_OCR_MAX_SIDE = 1400


def normalize_ocr_text(text: str) -> str:
    """Normalize high-value Japanese OCR confusions seen in focused retries."""
    corrections = {
        "よこやー": "よこせー", "よこわー": "よこせー", "4こやー": "よこせー",
        "4こせ！": "よこせ！", "41わ": "よこせー", "414ー": "よこせー",
        "何問題ない": "何も問題ない", "問題ない！": "問題ない！",
    }
    for broken, corrected in corrections.items():
        text = text.replace(broken, corrected)
    return text


def remove_repeated_phrase(text: str) -> str:
    """
    Remove lightweight inline repeated phrases caused by OCR chunking errors.
    Fixes cases like '全部オレに全部オレに' -> '全部オレに'.
    """
    text = text.strip()
    compact = "".join(text.split())
    if len(compact) < 4:
        return text

    for size in range(len(compact) // 2, 1, -1):
        collapsed = []
        index = 0
        changed = False
        while index < len(compact):
            phrase = compact[index:index + size]
            next_phrase = compact[index + size:index + size * 2]
            if len(phrase) == size and phrase == next_phrase:
                collapsed.append(phrase)
                index += size * 2
                changed = True
                while compact[index:index + size] == phrase:
                    index += size
            else:
                collapsed.append(compact[index])
                index += 1
        if changed:
            return "".join(collapsed)
    return text


def clean_ocr_text(text: str) -> str:
    return remove_repeated_phrase(normalize_ocr_text(text))


def _box_overlap(first: list[list[int]], second: list[list[int]]) -> float:
    first_x, first_y = [point[0] for point in first], [point[1] for point in first]
    second_x, second_y = [point[0] for point in second], [point[1] for point in second]
    ax1, ay1, ax2, ay2 = min(first_x), min(first_y), max(first_x), max(first_y)
    bx1, by1, bx2, by2 = min(second_x), min(second_y), max(second_x), max(second_y)
    intersection = max(0, min(ax2, bx2) - max(ax1, bx1)) * max(0, min(ay2, by2) - max(ay1, by1))
    first_area = max(1, (ax2 - ax1) * (ay2 - ay1))
    second_area = max(1, (bx2 - bx1) * (by2 - by1))
    return intersection / min(first_area, second_area)


def ocr_text_quality(text: str, confidence: float, model_language: str) -> float:
    suspicious_digits = any(char.isascii() and char.isdigit() for char in text)
    readable = min(len("".join(text.split())) / 20.0, 1.0)
    return confidence * 0.55 + script_fit(text, model_language) * 0.30 + readable * 0.15 - (0.25 if suspicious_digits else 0.0)


def retry_short_vertical_regions(
    ocr_engine, source: Path, regions: list[dict], model_language: str,
) -> list[dict]:
    """
    Retry vertical manga bubble regions where OCR only captured a partial column.
    Detects vertical regions (h > w * 1.5) with short text (<=3 chars) and re-runs
    OCR on a wider crop to capture adjacent columns in the same bubble.
    When multiple short vertical regions are close together, merges the crop area
    to cover the entire bubble at once.
    """
    from PIL import Image
    with Image.open(source) as opened:
        img_w, img_h = opened.size

    entries = [{**region, "text": clean_ocr_text(str(region["text"]))} for region in regions]

    # Find all vertical short regions
    short_verticals = []
    for idx, entry in enumerate(entries):
        text = str(entry["text"])
        xs = [point[0] for point in entry["polygon"]]
        ys = [point[1] for point in entry["polygon"]]
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        if h > w * 1.5 and len(text) <= 3:
            short_verticals.append((idx, entry, xs, ys, w, h))

    if not short_verticals:
        return entries

    # Build bubble-level crop areas by grouping nearby short verticals
    used = set()
    bubbles = []
    for a_idx, a_entry, a_xs, a_ys, a_w, a_h in short_verticals:
        if a_idx in used:
            continue
        used.add(a_idx)
        ax1, ay1, ax2, ay2 = min(a_xs), min(a_ys), max(a_xs), max(a_ys)
        bubble_x1, bubble_y1, bubble_x2, bubble_y2 = ax1, ay1, ax2, ay2

        for b_idx, b_entry, b_xs, b_ys, b_w, b_h in short_verticals:
            if b_idx in used:
                continue
            bx1, by1, bx2, by2 = min(b_xs), min(b_ys), max(b_xs), max(b_ys)
            # Same bubble if vertical overlap and close horizontally
            vert_overlap = max(0, min(ay2, by2) - max(ay1, by1))
            vert_span = max(ay2 - ay1, by2 - by1)
            gap = max(0, max(ax1, bx1) - min(ax2, bx2))
            if vert_overlap > 0 and gap < max(a_w, b_w) * 4:
                bubble_x1 = min(bubble_x1, bx1)
                bubble_y1 = min(bubble_y1, by1)
                bubble_x2 = max(bubble_x2, bx2)
                bubble_y2 = max(bubble_y2, by2)
                used.add(b_idx)

        bubble_width = bubble_x2 - bubble_x1
        bubble_height = bubble_y2 - bubble_y1
        # Expand further to capture fully missed columns
        expand_left = max(bubble_width * 2, 80)
        expand_right = max(bubble_width, 40)
        expand_bottom = max(8, bubble_height // 4)
        retry_rect = [
            max(0, bubble_x1 - expand_left),
            max(0, bubble_y1 - 4),
            min(img_w, bubble_x2 + expand_right),
            min(img_h, bubble_y2 + expand_bottom),
        ]
        bubbles.append((bubble_x1, bubble_y1, bubble_x2, bubble_y2, retry_rect))

    # Run one retry per bubble
    for bubble_x1, bubble_y1, bubble_x2, bubble_y2, retry_rect in bubbles:
        retry = ocr_engine.analyze_selection(
            source, retry_rect, preferred_language=model_language,
            add_context=True, rtl_context=True,
        )
        retry_regions = retry.to_dict()["regions"]
        if not retry_regions:
            continue

        new_entries = []
        for retry_r in retry_regions:
            retry_xs = [p[0] for p in retry_r["polygon"]]
            retry_ys = [p[1] for p in retry_r["polygon"]]
            rx1, ry1, rx2, ry2 = min(retry_xs), min(retry_ys), max(retry_xs), max(retry_ys)
            retry_text = clean_ocr_text(str(retry_r["text"]))
            retry_conf = float(retry_r["confidence"])

            if not retry_text.strip():
                continue

            # Find the best matching entry (by overlap)
            best_idx = -1
            best_overlap = 0
            for e_idx, entry in enumerate(entries):
                e_xs = [p[0] for p in entry["polygon"]]
                e_ys = [p[1] for p in entry["polygon"]]
                ex1, ey1, ex2, ey2 = min(e_xs), min(e_ys), max(e_xs), max(e_ys)
                inter = max(0, min(rx2, ex2) - max(rx1, ex1)) * max(0, min(ry2, ey2) - max(ry1, ey1))
                union_area = max(1, max(rx2, ex2) - min(rx1, ex1)) * max(1, max(ry2, ey2) - min(ry1, ey1))
                overlap = inter / union_area if union_area > 0 else 0
                if overlap > best_overlap:
                    best_overlap = overlap
                    best_idx = e_idx

            if best_idx >= 0 and best_overlap > 0.15:
                # Overlaps an existing entry - replace if better quality
                entry = entries[best_idx]
                original_quality = ocr_text_quality(str(entry["text"]), float(entry["confidence"]), model_language)
                candidate_quality = ocr_text_quality(retry_text, retry_conf, model_language)
                if candidate_quality > original_quality + 0.02:
                    entry.update(
                        text=retry_text,
                        confidence=retry_conf,
                        polygon=retry_r["polygon"],
                    )
            else:
                # New region not overlapping meaningfully with any existing entry
                new_entries.append({
                    "text": retry_text,
                    "confidence": retry_conf,
                    "polygon": retry_r["polygon"],
                })

        entries.extend(new_entries)

    return entries


def retry_suspicious_digit_regions(
    ocr_engine, source: Path, regions: list[dict], model_language: str,
) -> list[dict]:
    """Retry digit-like OCR fragments with wider context and keep only better text."""
    entries = [{**region, "text": clean_ocr_text(str(region["text"]))} for region in regions]
    for entry in entries:
        digits = sum(char.isascii() and char.isdigit() for char in str(entry["text"]))
        if digits < 3:
            continue
        xs, ys = [point[0] for point in entry["polygon"]], [point[1] for point in entry["polygon"]]
        x1, y1, x2, y2 = min(xs), min(ys), max(xs), max(ys)
        width, height = max(1, x2 - x1), max(1, y2 - y1)
        retry_rect = [max(0, x1 - width), max(0, y1 - height // 3), x2 + width, y2 + height // 2]
        retry = ocr_engine.analyze_selection(source, retry_rect, preferred_language=model_language)
        candidates = retry.to_dict()["regions"]
        if not candidates:
            continue
        target_center = ((x1 + x2) / 2, (y1 + y2) / 2)

        def distance(candidate):
            candidate_xs = [point[0] for point in candidate["polygon"]]
            candidate_ys = [point[1] for point in candidate["polygon"]]
            center = (
                (min(candidate_xs) + max(candidate_xs)) / 2,
                (min(candidate_ys) + max(candidate_ys)) / 2,
            )
            return abs(center[0] - target_center[0]) + abs(center[1] - target_center[1]) * 0.15

        candidate = {**min(candidates, key=distance)}
        candidate["text"] = clean_ocr_text(str(candidate["text"]))
        original_quality = ocr_text_quality(str(entry["text"]), float(entry["confidence"]), model_language)
        candidate_quality = ocr_text_quality(str(candidate["text"]), float(candidate["confidence"]), model_language)
        if candidate_quality > original_quality + 0.02:
            entry.update(text=candidate["text"], confidence=candidate["confidence"], polygon=candidate["polygon"])
    return entries


@dataclass
class TextRegion:
    text: str
    confidence: float
    polygon: list[list[int]]


@dataclass
class OCRResult:
    source: str
    model_language: str
    language: str
    language_confidence: float
    average_ocr_confidence: float
    regions: list[TextRegion]
    language_scripts: dict[str, int]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "OCRResult":
        return cls(
            source=str(payload.get("source", "")),
            model_language=str(payload.get("model_language", "")),
            language=str(payload.get("language", "unknown")),
            language_confidence=float(payload.get("language_confidence", 0.0)),
            average_ocr_confidence=float(payload.get("average_ocr_confidence", 0.0)),
            regions=[
                TextRegion(
                    str(region.get("text", "")),
                    float(region.get("confidence", 0.0)),
                    [[int(point[0]), int(point[1])] for point in region.get("polygon", [])],
                )
                for region in payload.get("regions", [])
            ],
            language_scripts=dict(payload.get("language_scripts", {})),
            metadata=dict(payload.get("metadata", {})),
        )


class PaddleOCREngine:
    _shared_engines: ClassVar[dict[str, object]] = {}
    _shared_engine_lock: ClassVar[threading.RLock] = threading.RLock()

    def __init__(self, languages: list[str]) -> None:
        self._languages = list(dict.fromkeys(languages))

    def _engine(self, language: str):
        with self._shared_engine_lock:
            engine = self._shared_engines.get(language)
            if engine is not None:
                return engine
            from paddleocr import PaddleOCR
            engine = PaddleOCR(
                lang=language,
                enable_mkldnn=False,
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=True,
            )
            self._shared_engines[language] = engine
            return engine

    @classmethod
    def clear_shared_engines(cls) -> None:
        with cls._shared_engine_lock:
            cls._shared_engines.clear()

    @staticmethod
    def _regions(payload: dict[str, Any]) -> list[TextRegion]:
        data = payload.get("res", payload)
        texts = data.get("rec_texts", [])
        scores = data.get("rec_scores", [])
        polygons = data.get("rec_polys", [])
        regions: list[TextRegion] = []
        for text, score, polygon in zip(texts, scores, polygons):
            cleaned = clean_ocr_text(str(text))
            if not cleaned.strip():
                continue
            candidate = TextRegion(
                cleaned,
                float(score),
                [[int(x), int(y)] for x, y in polygon],
            )
            duplicate_index = next((
                index for index, existing in enumerate(regions)
                if existing.text == candidate.text and _box_overlap(existing.polygon, candidate.polygon) >= 0.60
            ), None)
            if duplicate_index is None:
                regions.append(candidate)
            elif candidate.confidence > regions[duplicate_index].confidence:
                regions[duplicate_index] = candidate

        return regions

    @staticmethod
    def _candidate_score(regions: list[TextRegion], model_language: str) -> float:
        if not regions:
            return 0.0
        text = "\n".join(region.text for region in regions)
        average = sum(region.confidence for region in regions) / len(regions)
        readable = min(len("".join(text.split())) / 30.0, 1.0)
        return average * 0.65 + script_fit(text, model_language) * 0.25 + readable * 0.10

    @staticmethod
    def _remap_regions(regions: list[TextRegion], scale: float, offset: tuple[int, int]) -> list[TextRegion]:
        offset_x, offset_y = offset
        return [TextRegion(
            region.text, region.confidence,
            [[round(x / scale) + offset_x, round(y / scale) + offset_y] for x, y in region.polygon],
        ) for region in regions]

    def analyze(self, image_path: Path, preferred_language: str | None = None) -> OCRResult:
        candidates: list[tuple[float, str, list[TextRegion]]] = []
        languages = [preferred_language] if preferred_language in self._languages else self._languages
        metadata: dict[str, Any] = {
            "requested_preferred_language": preferred_language,
            "model_languages_tried": languages,
            "full_page_max_side": FULL_PAGE_OCR_MAX_SIDE,
        }
        with tempfile.TemporaryDirectory(prefix="hydra-ocr-page-") as temporary:
            folder = Path(temporary)
            ocr_path = image_path
            scale = 1.0
            with Image.open(image_path) as opened:
                width, height = opened.size
                longest = max(width, height)
                metadata["input_size"] = [width, height]
                if longest > FULL_PAGE_OCR_MAX_SIDE:
                    scale = FULL_PAGE_OCR_MAX_SIDE / longest
                    resized = opened.convert("RGB").resize(
                        (max(1, round(width * scale)), max(1, round(height * scale))),
                        Image.Resampling.LANCZOS,
                    )
                    ocr_path = folder / "page.png"
                    resized.save(ocr_path)
                    metadata["ocr_size"] = [resized.width, resized.height]
                else:
                    metadata["ocr_size"] = [width, height]
                metadata["full_page_scale"] = scale
                metadata["resized_for_ocr"] = scale != 1.0

            for model_language in languages:
                engine = self._engine(model_language)
                predictions = list(engine.predict(str(ocr_path)))
                regions = self._regions(predictions[0].json) if predictions else []
                if scale != 1.0:
                    regions = self._remap_regions(regions, scale, (0, 0))
                score = self._candidate_score(regions, model_language)
                candidates.append((score, model_language, regions))

        _, model_language, regions = max(candidates, key=lambda candidate: candidate[0])
        combined_text = "\n".join(region.text for region in regions)
        evidence = detect_language(combined_text)
        average = sum(region.confidence for region in regions) / len(regions) if regions else 0.0
        return OCRResult(
            source=str(image_path.resolve()),
            model_language=model_language,
            language=evidence.language,
            language_confidence=evidence.confidence,
            average_ocr_confidence=average,
            regions=regions,
            language_scripts=evidence.scripts,
            metadata=metadata,
        )

    def analyze_selection(
        self, image_path: Path, rect: list[int], *, preferred_language: str | None = None,
        add_context: bool = False, rtl_context: bool = False,
    ) -> OCRResult:
        """Retry OCR on an upscaled crop and return polygons in page coordinates."""
        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
        x1, y1, x2, y2 = [int(value) for value in rect]
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(image.width, x2), min(image.height, y2)
        if add_context:
            width, height = max(1, x2 - x1), max(1, y2 - y1)
            left_pad = max(12, width)
            right_pad = max(6, width // 4) if rtl_context else left_pad
            top_pad = max(4, height // 8) if height < 50 else 8
            bottom_pad = max(8, round(height * 1.25)) if height < 50 else 8
            x1, y1 = max(0, x1 - left_pad), max(0, y1 - top_pad)
            x2, y2 = min(image.width, x2 + right_pad), min(image.height, y2 + bottom_pad)
        crop = image.crop((x1, y1, x2, y2))
        languages = [preferred_language] if preferred_language in self._languages else self._languages
        candidates: list[tuple[float, int, str, int, list[TextRegion]]] = []
        with tempfile.TemporaryDirectory(prefix="hydra-ocr-retry-") as temporary:
            folder = Path(temporary)
            color_2x = crop.resize((crop.width * 2, crop.height * 2), Image.Resampling.LANCZOS)
            gray_2x = ImageOps.autocontrast(color_2x.convert("L"))
            gray_3x = ImageOps.autocontrast(crop.resize((crop.width * 3, crop.height * 3), Image.Resampling.LANCZOS).convert("L"))
            variants = [("color2", color_2x, 2, 0), ("gray2", gray_2x, 2, 1), ("gray3", gray_3x, 3, 2), ("original", crop, 1, 1)]
            for name, variant, scale, priority in variants:
                variant_path = folder / f"{name}.png"
                variant.save(variant_path)
                for model_language in languages:
                    predictions = list(self._engine(model_language).predict(str(variant_path)))
                    raw_regions = self._regions(predictions[0].json) if predictions else []
                    regions = self._remap_regions(raw_regions, scale, (x1, y1))
                    # Prefer the color retry when candidates are effectively tied;
                    # grayscale sometimes turns kana strokes into plausible but wrong glyphs.
                    score = self._candidate_score(regions, model_language) - priority * 0.015
                    candidates.append((score, -priority, model_language, scale, regions))
        _, _, model_language, _, regions = max(candidates, key=lambda candidate: (candidate[0], candidate[1]))
        combined_text = "\n".join(region.text for region in regions)
        evidence = detect_language(combined_text)
        average = sum(region.confidence for region in regions) / len(regions) if regions else 0.0
        return OCRResult(
            source=str(image_path.resolve()), model_language=model_language,
            language=evidence.language, language_confidence=evidence.confidence,
            average_ocr_confidence=average, regions=regions, language_scripts=evidence.scripts,
        )
