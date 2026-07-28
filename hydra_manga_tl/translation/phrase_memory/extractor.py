"""Deterministic sub-phrase extraction and alignment for Phrase Memory (PM v1)."""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable

# CJK range check for Japanese / Chinese Kanji & Kana
_CJK_CHAR_RE = re.compile(
    r"[\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff\u3400-\u4dbf]+"
)

# Common generic stop words to exclude when identifying terms
_STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for", "of",
    "with", "by", "from", "up", "about", "into", "over", "after", "is", "are",
    "was", "were", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "will", "would", "shall", "should", "may", "might", "must", "can",
    "could", "me", "my", "you", "your", "he", "him", "his", "she", "her", "it",
    "its", "we", "us", "our", "they", "them", "their", "this", "that", "these",
    "those", "very", "today", "now", "here", "there", "what", "where", "when",
    "who", "how", "why", "yes", "no", "not", "so", "just", "like", "than",
}


def normalize_phrase(text: str) -> str:
    """Normalize phrase text removing linebreaks, NFKC folding, stripping excess spaces."""
    val = str(text).replace("\r\n", "\n").replace("\r", "\n")
    val = unicodedata.normalize("NFKC", val)
    val = re.sub(r"\s+", " ", val).strip()
    return val.strip("！？。!?…,\". ")


def is_cjk_term(text: str) -> bool:
    """Check if string contains CJK characters (Japanese / Chinese)."""
    return any(
        "\u3040" <= char <= "\u30ff"
        or "\u4e00" <= char <= "\u9fff"
        or "\u3400" <= char <= "\u4dbf"
        for char in text
    )


def extract_cjk_candidate_phrases(source_text: str) -> list[str]:
    """Extract candidate CJK noun/term phrases (2 to 8 CJK characters)."""
    candidates: list[str] = []
    clean_text = normalize_phrase(source_text)
    cjk_blocks = _CJK_CHAR_RE.findall(clean_text)

    for block in cjk_blocks:
        length = len(block)
        if 2 <= length <= 8:
            candidates.append(block)
        elif length > 8:
            # Generate 2 to 6 character n-grams from longer CJK blocks
            for n in range(2, min(7, length + 1)):
                for i in range(length - n + 1):
                    ngram = block[i : i + n]
                    candidates.append(ngram)

    # Deduplicate preserving order
    seen: set[str] = set()
    result: list[str] = []
    for cand in candidates:
        if cand not in seen and len(cand) >= 2:
            seen.add(cand)
            result.append(cand)
    return result


def extract_aligned_phrases(
    source_text: str,
    target_text: str,
    explicit_glossary: dict[str, str] | None = None,
) -> list[tuple[str, str]]:
    """Extract valid (source_phrase, target_phrase) tuples from a validated translation pair.

    1. Include explicit glossary terms present in source and target.
    2. Extract CJK sub-phrases or short source/target term pairs deterministically.
    """
    results: list[tuple[str, str]] = []
    norm_source = normalize_phrase(source_text)
    norm_target = normalize_phrase(target_text)

    if not norm_source or not norm_target:
        return results

    # 1. Include explicit glossary terms present in source and target
    if explicit_glossary:
        for src, tgt in explicit_glossary.items():
            clean_src = normalize_phrase(src)
            clean_tgt = normalize_phrase(tgt)
            if clean_src and clean_tgt and clean_src in norm_source and clean_tgt in norm_target:
                results.append((clean_src, clean_tgt))

    # 2. Extract compact candidate CJK phrases if source is a short term itself
    if is_cjk_term(norm_source) and 2 <= len(norm_source) <= 8 and len(norm_target.split()) <= 4:
        results.append((norm_source, norm_target))

    # 3. Candidate sub-phrases inside longer source text
    candidates = extract_cjk_candidate_phrases(norm_source)
    for cand in candidates:
        if explicit_glossary and cand in explicit_glossary:
            tgt = explicit_glossary[cand]
            if tgt in norm_target:
                results.append((cand, tgt))

    # Deduplicate results
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for src, tgt in results:
        clean_s = src.strip()
        clean_t = tgt.strip()
        if not clean_s or not clean_t:
            continue
        if (clean_s, clean_t) not in seen and clean_t.casefold() not in _STOP_WORDS:
            seen.add((clean_s, clean_t))
            unique.append((clean_s, clean_t))
    return unique

