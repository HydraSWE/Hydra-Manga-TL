"""Spatial grouping of OCR regions into translation units."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .ocr import clean_ocr_text
from .language import script_fit


@dataclass
class TextGroup:
    member_indices: list[int]
    text: str
    bbox: list[int]
    direction: str


@dataclass(frozen=True)
class GroupClassification:
    kind: str
    confidence: float
    reasons: list[str]


def _box(region: dict) -> tuple[int, int, int, int]:
    xs = [point[0] for point in region["polygon"]]
    ys = [point[1] for point in region["polygon"]]
    return min(xs), min(ys), max(xs), max(ys)


def _overlap(start_a: int, end_a: int, start_b: int, end_b: int) -> float:
    intersection = max(0, min(end_a, end_b) - max(start_a, start_b))
    return intersection / max(1, min(end_a - start_a, end_b - start_b))


def _is_tiny_artifact(text: str, w: int, h: int) -> bool:
    """Patch 4: Filter ONLY when text is devoid of alphanumeric content AND is physically tiny."""
    clean_text = re.sub(r'[.,!?\"\'\(\)\[\]\{\}<>\-_~= ]', '', str(text))
    # If it has actual text, keep it. If it's punctuation but normal-sized, keep it.
    if not clean_text and w <= 15 and h <= 15:
        return True
    return False


def is_noise(text: str) -> bool:
    """Filter short numeric-only text or text ending with prolonged sound mark (ー)."""
    stripped = text.strip()
    compact = re.sub(r"\s+", "", stripped)
    
    if len(stripped) <= 2 and any(c.isdigit() for c in stripped):
        return True
        
    if len(stripped) <= 4 and stripped.endswith("ー") and any(c.isdigit() for c in stripped.replace("ー", "")):
        return True

    digits = sum(c.isdigit() for c in compact)
    if len(compact) >= 3 and digits / max(1, len(compact)) >= 0.45:
        return True

    if len(compact) <= 3 and compact.isascii() and any(c.isalpha() for c in compact):
        return True
        
    return False


def _compact_text(text: str) -> str:
    return re.sub(r"\s+", "", str(text).strip())


def _script_counts(text: str) -> dict[str, int]:
    compact = _compact_text(text)
    return {
        "kana": sum(1 for c in compact if "\u3040" <= c <= "\u30ff"),
        "cjk": sum(1 for c in compact if "\u3400" <= c <= "\u9fff"),
        "latin": sum(1 for c in compact if c.isascii() and c.isalpha()),
        "digits": sum(1 for c in compact if c.isascii() and c.isdigit()),
        "symbols": sum(1 for c in compact if _is_decorative_symbol(c)),
    }


def _is_decorative_symbol(char: str) -> bool:
    codepoint = ord(char)
    if char in "♪♫♬♩♡♥❤★☆◇◆■□●○◎※〆〒":
        return True
    if unicodedata.category(char).startswith("S"):
        return True
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or 0xFE00 <= codepoint <= 0xFE0F
    )


def _looks_like_logo_or_sticker(compact: str, width: int, height: int, counts: dict[str, int], confidence: float) -> bool:
    jp_count = counts["kana"] + counts["cjk"]
    symbol_count = counts["symbols"]
    non_jp = len(compact) - jp_count
    aspect = width / max(1, height)
    if symbol_count and (len(compact) <= 8 or symbol_count >= max(1, jp_count)):
        return True
    if aspect >= 2.8 and len(compact) <= 10 and jp_count <= 4:
        return True
    if aspect >= 2.2 and confidence < 0.75 and non_jp >= jp_count:
        return True
    return False


def _member_confidence(group: TextGroup, regions: list[dict] | None) -> float:
    if not regions:
        return 1.0
    values = []
    for index in group.member_indices:
        if 1 <= index <= len(regions):
            values.append(float(regions[index - 1].get("confidence", 1.0)))
    return min(values) if values else 1.0


def classify_text_group(
    group: TextGroup, regions: list[dict] | None = None, model_language: str = "japan",
) -> GroupClassification:
    """Conservatively decide whether an OCR group should be translated.

    The classifier protects the visible page from obvious OCR junk/SFX while
    keeping uncertain but meaningful CJK text available for review.
    """
    text = str(group.text).strip()
    compact = _compact_text(text)
    x1, y1, x2, y2 = group.bbox
    width, height = max(1, x2 - x1), max(1, y2 - y1)
    counts = _script_counts(compact)
    jp_count = counts["kana"] + counts["cjk"]
    confidence = _member_confidence(group, regions)
    reasons: list[str] = []

    if not compact:
        return GroupClassification("noise", confidence, ["empty_ocr"])
    if is_noise(compact):
        return GroupClassification("noise", confidence, ["text_noise_pattern"])
    if counts["latin"] and not jp_count and len(compact) <= 3:
        return GroupClassification("noise", confidence, ["short_latin_noise"])
    if confidence < 0.55 and jp_count == 0:
        return GroupClassification("noise", confidence, ["low_confidence_non_cjk"])
    if confidence < 0.45 and len(compact) <= 4:
        return GroupClassification("noise", confidence, ["low_confidence_short_text"])
    if _looks_like_logo_or_sticker(compact, width, height, counts, confidence):
        return GroupClassification("decorative", confidence, ["logo_sticker_or_symbol"])

    clean = compact.strip(".,!?！？。…・ッっー~〜-")
    if len(clean) <= 1 and jp_count:
        return GroupClassification("sfx", confidence, ["isolated_short_japanese"])
    if len(compact) <= 5 and counts["kana"] >= max(1, len(clean) - 1) and (
        compact.endswith(("ッ", "っ", "ー", "！", "!")) or height > width * 1.4
    ):
        return GroupClassification("sfx", confidence, ["short_kana_sound_effect"])
    if len(compact) <= 3 and jp_count and confidence < 0.80 and script_fit(compact, model_language) < 0.70:
        return GroupClassification("sfx", confidence, ["ambiguous_short_low_confidence"])

    if confidence < 0.70:
        reasons.append("ambiguous_low_confidence")
    if height > width * 1.35 and jp_count >= 2:
        return GroupClassification("dialogue", confidence, reasons)
    if jp_count >= 2 or len(compact) >= 6:
        return GroupClassification("dialogue", confidence, reasons)
    return GroupClassification("noise", confidence, reasons or ["unreadable_short_group"])


def _build_reading_graph(groups: list[TextGroup]) -> list[TextGroup]:
    """Patch 3: Dynamic tier-based reading order graph."""
    if not groups:
        return []
        
    # Calculate a dynamic tier height based on the median text group height
    heights = [g.bbox[3] - g.bbox[1] for g in groups]
    median_h = sorted(heights)[len(heights) // 2]
    tier_height = max(20, median_h * 1.25)
    
    return sorted(groups, key=lambda g: (g.bbox[1] // tier_height, -g.bbox[2]))


def group_regions(regions: list[dict]) -> list[TextGroup]:
    """Group neighboring vertical columns; leave horizontal OCR lines intact."""
    valid_indices = []
    for index, region in enumerate(regions):
        x1, y1, x2, y2 = _box(region)
        if not _is_tiny_artifact(region.get("text", ""), x2 - x1, y2 - y1):
            valid_indices.append(index)

    # Filter noise regions (short numeric-only text, noise artifacts)
    valid_indices = [
        i for i in valid_indices
        if not is_noise(regions[i].get("text", ""))
    ]

    boxes = {i: _box(regions[i]) for i in valid_indices}
    vertical = {}

    for i in valid_indices:
        x1, y1, x2, y2 = boxes[i]

        w = x2 - x1
        h = y2 - y1

        text = str(regions[i].get("text", ""))

        japanese_chars = sum(
            1 for c in text
            if "\u3040" <= c <= "\u30ff" or "\u3400" <= c <= "\u9fff"
        )

        # Strong vertical evidence:
        # - substantially taller than wide
        # - multiple Japanese characters
        # We intentionally avoid a strict width cap because manga bubbles can be narrow or moderately wide.
        vertical[i] = (
            h > w * 1.35
            and japanese_chars >= 2
        )

    parents = {i: i for i in valid_indices}

    def find(item: int) -> int:
        while parents[item] != item:
            parents[item] = parents[parents[item]]
            item = parents[item]
        return item

    def union(a: int, b: int) -> None:
        a_root, b_root = find(a), find(b)
        if a_root != b_root:
            parents[b_root] = a_root

    for idx, left in enumerate(valid_indices):
        if not vertical[left]:
            continue
        lx1, ly1, lx2, ly2 = boxes[left]
        lx_center = (lx1 + lx2) / 2
        
        for right in valid_indices[idx + 1:]:
            if not vertical[right]:
                continue
            rx1, ry1, rx2, ry2 = boxes[right]
            rx_center = (rx1 + rx2) / 2
            
            horizontal_gap = max(0, max(lx1, rx1) - min(lx2, rx2))
            typical_width = min(lx2 - lx1, rx2 - rx1)
            
            center_difference = abs(lx_center - rx_center)
            # Manga vertical bubble grouping: relaxed thresholds for Japanese columns
            # that are separated horizontally within the same speech bubble.
            same_bubble = (
                _overlap(ly1, ly2, ry1, ry2) >= 0.45
                and horizontal_gap <= typical_width * 2
                and center_difference < typical_width * 3
            )
            if same_bubble:
                union(left, right)

    members: dict[int, list[int]] = {}
    for index in valid_indices:
        members.setdefault(find(index), []).append(index)

    groups: list[TextGroup] = []
    for indices in members.values():
        is_vertical = all(vertical[index] for index in indices)
        ordered = sorted(
            indices,
            key=(lambda index: (-boxes[index][0], boxes[index][1])) if is_vertical else (lambda index: (boxes[index][1], boxes[index][0])),
        )
        x1 = min(boxes[index][0] for index in indices)
        y1 = min(boxes[index][1] for index in indices)
        x2 = max(boxes[index][2] for index in indices)
        y2 = max(boxes[index][3] for index in indices)
        groups.append(TextGroup(
            member_indices=[index + 1 for index in ordered],
            text=clean_ocr_text("".join(str(regions[index]["text"]) for index in ordered)),
            bbox=[x1, y1, x2, y2],
            direction="vertical-rtl" if is_vertical else "horizontal-ltr",
        ))
        
    return _build_reading_graph(groups)


def compose_manual_region(regions: list[dict]) -> TextGroup:
    """Compose every OCR result in an explicit user selection as one unit."""
    if not regions:
        raise ValueError("No text was detected in the selected area.")
    boxes = [_box(region) for region in regions]
    vertical_count = 0
    for region, (x1, y1, x2, y2) in zip(regions, boxes):
        w = x2 - x1
        h = y2 - y1
        text = str(region.get("text", ""))

        jp = sum(
            1 for c in text
            if ("\u3040" <= c <= "\u30ff") or ("\u3400" <= c <= "\u9fff")
        )

        if h > w * 1.35 and jp >= 2:
            vertical_count += 1

    is_vertical = vertical_count > len(regions) / 2
    ordered = sorted(
        range(len(regions)),
        key=(lambda index: (-boxes[index][0], boxes[index][1])) if is_vertical else (lambda index: (boxes[index][1], boxes[index][0])),
    )
    return TextGroup(
        member_indices=[index + 1 for index in ordered],
        text=clean_ocr_text("".join(str(regions[index]["text"]) for index in ordered)),
        bbox=[
            min(box[0] for box in boxes), min(box[1] for box in boxes),
            max(box[2] for box in boxes), max(box[3] for box in boxes),
        ],
        direction="vertical-rtl" if is_vertical else "horizontal-ltr",
    )
