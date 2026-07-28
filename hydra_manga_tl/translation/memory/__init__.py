"""Hydra Translation Memory v1 public API."""

from .database import (
    SCHEMA_VERSION,
    TRANSLATION_MEMORY,
    TranslationMemory,
    TranslationMemoryDatabase,
    TranslationMemoryMatcher,
)
from .fingerprints import (
    REGION_HASH_PREFIX,
    TEXT_HASH_PREFIX,
    normalize_tm_source_text,
    source_region_hash,
    source_text_hash,
)
from .models import (
    TranslationMemoryEntry,
    TranslationMemoryMatch,
    TranslationMemoryStatistics,
)
from .learning import learn_validated_page

__all__ = [
    "REGION_HASH_PREFIX",
    "SCHEMA_VERSION",
    "TEXT_HASH_PREFIX",
    "TRANSLATION_MEMORY",
    "TranslationMemory",
    "TranslationMemoryDatabase",
    "TranslationMemoryEntry",
    "TranslationMemoryMatch",
    "TranslationMemoryMatcher",
    "TranslationMemoryStatistics",
    "normalize_tm_source_text",
    "learn_validated_page",
    "source_region_hash",
    "source_text_hash",
]
