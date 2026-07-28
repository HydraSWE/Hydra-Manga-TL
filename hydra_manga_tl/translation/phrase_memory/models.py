"""Data models for Hydra Phrase Memory (PM v1)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Any


@dataclass
class PhraseMemoryEntry:
    id: int | None
    source_phrase: str
    normalized_phrase: str
    source_phrase_hash: str
    target_phrase: str
    source_language: str
    target_language: str
    verified: bool = False
    created_at: str = ""
    updated_at: str = ""
    usage_count: int = 0
    confidence: float = 1.0
    origin: str = "QA"
    project_id: str | None = None
    series_id: str | None = None
    notes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PhraseMemoryEntry:
        known = {field.name for field in fields(cls)}
        filtered = {k: v for k, v in data.items() if k in known}
        return cls(**filtered)


@dataclass(frozen=True)
class PhraseMemoryMatch:
    entry: PhraseMemoryEntry
    match_type: str = "exact-phrase"
    source: str = "phrase-memory"

    @property
    def source_phrase(self) -> str:
        return self.entry.source_phrase

    @property
    def target_phrase(self) -> str:
        return self.entry.target_phrase


@dataclass(frozen=True)
class PhraseMemoryStatistics:
    total_entries: int = 0
    verified_entries: int = 0
    total_matches: int = 0
    learned_count: int = 0
    saved_api_cost: float = 0.0
    saved_time_seconds: float = 0.0
