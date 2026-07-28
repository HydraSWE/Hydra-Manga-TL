"""Phrase Memory service API and global instance management (PM v1)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from hydra_manga_tl.core.paths import PATHS
from .database import PhraseMemoryDatabase
from .extractor import extract_aligned_phrases
from .matcher import PhraseMemoryMatcher
from .models import (
    PhraseMemoryEntry,
    PhraseMemoryMatch,
    PhraseMemoryStatistics,
)


class PhraseMemory:
    """High-level Phrase Memory API with lifecycle management and prompt helpers."""

    def __init__(self, path: Path | None = None) -> None:
        self.database = PhraseMemoryDatabase(path or PATHS.phrase_memory)
        self.matcher = PhraseMemoryMatcher(self.database)

    def configure(self, path: Path | None = None) -> None:
        if path is not None and Path(path) != self.database.path:
            self.database = PhraseMemoryDatabase(Path(path))
            self.matcher = PhraseMemoryMatcher(self.database)

    def lookup_phrase(
        self,
        *,
        source_phrase: str,
        source_language: str,
        target_language: str,
        prefer_verified: bool = True,
        record_usage: bool = True,
    ) -> PhraseMemoryMatch | None:
        return self.database.lookup_phrase(
            source_phrase=source_phrase,
            source_language=source_language,
            target_language=target_language,
            prefer_verified=prefer_verified,
            record_usage=record_usage,
        )

    def find_terminology_for_page(
        self,
        dialogues: Iterable[dict],
        source_language: str,
        target_language: str,
        *,
        glossary: dict[str, str] | None = None,
        prefer_verified: bool = True,
    ) -> dict[str, str]:
        """Return non-conflicting {source_phrase: target_phrase} mappings for dialogues."""
        return self.matcher.find_matches_for_page(
            dialogues=dialogues,
            source_language=source_language,
            target_language=target_language,
            glossary=glossary,
            prefer_verified=prefer_verified,
        )

    def record(
        self,
        *,
        source_phrase: str,
        target_phrase: str,
        source_language: str,
        target_language: str,
        verified: bool = False,
        confidence: float = 1.0,
        origin: str = "QA",
        project_id: str | None = None,
        series_id: str | None = None,
        notes: str | None = None,
    ) -> PhraseMemoryEntry | None:
        return self.database.record(
            source_phrase=source_phrase,
            target_phrase=target_phrase,
            source_language=source_language,
            target_language=target_language,
            verified=verified,
            confidence=confidence,
            origin=origin,
            project_id=project_id,
            series_id=series_id,
            notes=notes,
        )

    def record_user_edit(
        self,
        *,
        source_phrase: str,
        target_phrase: str,
        source_language: str,
        target_language: str,
        project_id: str | None = None,
        series_id: str | None = None,
    ) -> PhraseMemoryEntry | None:
        return self.database.record_user_edit(
            source_phrase=source_phrase,
            target_phrase=target_phrase,
            source_language=source_language,
            target_language=target_language,
            project_id=project_id,
            series_id=series_id,
        )

    def learn_page_phrases(
        self,
        dialogues: Iterable[dict],
        translations: Iterable[dict],
        source_language: str,
        target_language: str,
        *,
        valid_ids: Iterable[str] | None = None,
        glossary: dict[str, str] | None = None,
        project_id: str | None = None,
        series_id: str | None = None,
        origin: str = "QA",
        verified: bool = False,
    ) -> int:
        """Extract and store phrases from validated dialogue/translation pairs."""
        allowed = {str(v) for v in valid_ids} if valid_ids is not None else None
        trans_by_id = {
            str(item.get("id", "")): str(item.get("text", ""))
            for item in translations
        }

        learned_count = 0
        for item in dialogues:
            entry_id = str(item.get("id", ""))
            if allowed is not None and entry_id not in allowed:
                continue
            src_text = str(item.get("text", "")).strip()
            tgt_text = trans_by_id.get(entry_id, "").strip()
            if not src_text or not tgt_text:
                continue

            extracted = extract_aligned_phrases(
                src_text, tgt_text, explicit_glossary=glossary
            )
            for src_p, tgt_p in extracted:
                entry = self.record(
                    source_phrase=src_p,
                    target_phrase=tgt_p,
                    source_language=source_language,
                    target_language=target_language,
                    verified=verified,
                    origin=origin,
                    project_id=project_id,
                    series_id=series_id,
                )
                if entry is not None:
                    learned_count += 1

        return learned_count

    def format_prompt_terminology(
        self,
        terminology: dict[str, str],
    ) -> str:
        """Format matched phrases as prompt terminology hints."""
        if not terminology:
            return ""
        lines = [f"{src} → {tgt}" for src, tgt in terminology.items()]
        return "Known terminology:\n" + "\n".join(lines)

    def statistics(self) -> PhraseMemoryStatistics:
        return self.database.statistics()

    def clear(self) -> None:
        self.database.clear()

    def export(self, destination: Path) -> Path:
        return self.database.export(destination)

    def import_file(self, source: Path) -> int:
        return self.database.import_file(source)

    def record_entry_hits(self, entry_ids: Iterable[int]) -> None:
        self.database.record_hits(entry_ids)

    def delete_entry(self, entry_id: int) -> bool:
        return self.database.delete_entry(entry_id)

    def update_entry(
        self,
        entry_id: int,
        *,
        source_phrase: str | None = None,
        target_phrase: str | None = None,
        verified: bool | None = None,
        notes: str | None = None,
    ) -> bool:
        return self.database.update_entry(
            entry_id,
            source_phrase=source_phrase,
            target_phrase=target_phrase,
            verified=verified,
            notes=notes,
        )

    def toggle_verified(self, entry_id: int) -> bool:
        return self.database.toggle_verified(entry_id)

    def all_entries(self) -> list[PhraseMemoryEntry]:
        return self.database.all_entries()


PHRASE_MEMORY = PhraseMemory()

