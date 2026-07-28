"""Unicode-script language evidence for OCR results."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import unicodedata


@dataclass(frozen=True)
class LanguageEvidence:
    language: str
    confidence: float
    scripts: dict[str, int]


def resolve_source_language(project_source: str, page_source: str = "", block_source: str = "") -> str:
    """Choose the source language using the user's explicit project choice first."""
    project_source = str(project_source or "").strip()
    if project_source and project_source.casefold() != "auto":
        return project_source
    return str(page_source or "").strip() or str(block_source or "").strip()


def _script(char: str) -> str | None:
    code = ord(char)
    if 0x3040 <= code <= 0x30FF:
        return "kana"
    if 0xAC00 <= code <= 0xD7AF:
        return "hangul"
    if 0x4E00 <= code <= 0x9FFF or 0x3400 <= code <= 0x4DBF:
        return "han"
    if 0x0400 <= code <= 0x052F:
        return "cyrillic"
    if 0x0600 <= code <= 0x06FF:
        return "arabic"
    if char.isalpha() and "LATIN" in unicodedata.name(char, ""):
        return "latin"
    return None


def detect_language(text: str) -> LanguageEvidence:
    counts = Counter(filter(None, (_script(char) for char in text)))
    total = sum(counts.values())
    if not total:
        return LanguageEvidence("unknown", 0.0, dict(counts))

    # Require a meaningful script share so one low-confidence OCR glyph cannot
    # override the dominant language of an entire page.
    meaningful = max(2, round(total * 0.10))
    if counts["kana"] >= meaningful:
        language, relevant = "Japanese", counts["kana"] + counts["han"]
    elif counts["hangul"] >= meaningful:
        language, relevant = "Korean", counts["hangul"] + counts["han"]
    elif counts["han"] > counts["latin"]:
        language, relevant = "Chinese", counts["han"]
    elif counts["cyrillic"]:
        language, relevant = "Russian/Cyrillic", counts["cyrillic"]
    elif counts["arabic"]:
        language, relevant = "Arabic", counts["arabic"]
    else:
        language, relevant = "Latin-script", counts["latin"]
    return LanguageEvidence(language, relevant / total, dict(counts))


def script_fit(text: str, model_language: str) -> float:
    evidence = detect_language(text)
    expected = {
        "japan": {"kana", "han", "latin"},
        "ch": {"han", "latin"},
        "korean": {"hangul", "han", "latin"},
        "en": {"latin"},
    }.get(model_language, set(evidence.scripts))
    total = sum(evidence.scripts.values())
    return sum(value for key, value in evidence.scripts.items() if key in expected) / total if total else 0.0
