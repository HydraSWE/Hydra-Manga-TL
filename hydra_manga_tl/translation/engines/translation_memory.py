"""Backwards-compatible import path for Hydra Translation Memory v1."""

from hydra_manga_tl.translation.memory import (
    TRANSLATION_MEMORY,
    TranslationMemory,
    TranslationMemoryDatabase,
    TranslationMemoryEntry,
    TranslationMemoryMatch,
    TranslationMemoryMatcher,
    TranslationMemoryStatistics,
    normalize_tm_source_text,
    source_region_hash,
    source_text_hash,
)

__all__ = [
    "TRANSLATION_MEMORY",
    "TranslationMemory",
    "TranslationMemoryDatabase",
    "TranslationMemoryEntry",
    "TranslationMemoryMatch",
    "TranslationMemoryMatcher",
    "TranslationMemoryStatistics",
    "normalize_tm_source_text",
    "source_region_hash",
    "source_text_hash",
]
