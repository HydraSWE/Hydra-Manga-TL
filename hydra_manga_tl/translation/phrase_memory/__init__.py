"""Hydra Phrase Memory v1 public API."""

from .database import (
    PHRASE_HASH_PREFIX,
    SCHEMA_VERSION,
    PhraseMemoryDatabase,
    source_phrase_hash,
)
from .extractor import (
    extract_aligned_phrases,
    extract_cjk_candidate_phrases,
    normalize_phrase,
)
from .matcher import PhraseMemoryMatcher
from .models import (
    PhraseMemoryEntry,
    PhraseMemoryMatch,
    PhraseMemoryStatistics,
)
from .service import PHRASE_MEMORY, PhraseMemory

__all__ = [
    "PHRASE_HASH_PREFIX",
    "PHRASE_MEMORY",
    "SCHEMA_VERSION",
    "PhraseMemory",
    "PhraseMemoryDatabase",
    "PhraseMemoryEntry",
    "PhraseMemoryMatch",
    "PhraseMemoryMatcher",
    "PhraseMemoryStatistics",
    "extract_aligned_phrases",
    "extract_cjk_candidate_phrases",
    "normalize_phrase",
    "source_phrase_hash",
]

