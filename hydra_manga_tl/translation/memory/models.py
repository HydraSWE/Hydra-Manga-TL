"""Translation-memory data contracts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class TranslationMemoryEntry:
    id: int | None
    source_text: str
    normalized_text: str
    source_text_hash: str
    source_region_hash: str | None
    translated_text: str
    source_language: str
    target_language: str
    region_type: str
    translation_provider: str = ""
    provider_model: str = ""
    created_at: str = ""
    last_used_at: str = ""
    usage_count: int = 0
    verified: bool = False
    user_edited: bool = False
    quality_score: float = 0.0
    origin: str = "provider"
    series_id: str | None = None
    glossary_version: str | None = None
    project_id: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TranslationMemoryMatch:
    entry: TranslationMemoryEntry
    match_type: str = "exact"
    source: str = "translation-memory"

    @property
    def translated_text(self) -> str:
        return self.entry.translated_text


@dataclass(frozen=True)
class TranslationMemoryStatistics:
    total_entries: int = 0
    exact_matches: int = 0
    provider_calls_saved: int = 0
    estimated_api_cost_saved: float = 0.0
    estimated_time_saved_seconds: float = 0.0
    verified_entries: int = 0
    user_edited_entries: int = 0
    imported_entries: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
