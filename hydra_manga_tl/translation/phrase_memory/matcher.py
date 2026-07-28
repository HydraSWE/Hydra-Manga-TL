"""Phrase Memory matching and candidate ranking logic (PM v1)."""

from __future__ import annotations

from typing import Iterable, Sequence

from .database import PhraseMemoryDatabase, source_phrase_hash
from .models import PhraseMemoryEntry, PhraseMemoryMatch


class PhraseMemoryMatcher:
    """Matches text bubbles against stored Phrase Memory entries with Glossary overrides."""

    def __init__(self, database: PhraseMemoryDatabase) -> None:
        self.database = database

    def find_matches_in_text(
        self,
        source_text: str,
        source_language: str,
        target_language: str,
        *,
        glossary: dict[str, str] | None = None,
        prefer_verified: bool = True,
    ) -> list[PhraseMemoryMatch]:
        """Find non-conflicting phrase memory matches within source_text.

        Glossary entries take absolute priority. If a source phrase is already
        in the glossary, the Phrase Memory entry for that phrase is ignored.
        """
        if not source_text or not source_text.strip():
            return []

        glossary_keys = {
            str(k).strip().casefold()
            for k in (glossary or {}).keys()
            if str(k).strip()
        }

        # Fetch all candidate entries for this language pair
        all_entries = self.database.all_entries_for_languages(
            source_language=source_language,
            target_language=target_language,
        )

        matched: list[PhraseMemoryMatch] = []
        seen_phrases: set[str] = set()

        # Sort entries: longer phrase first, then verified, then usage_count, then updated_at
        sorted_entries = sorted(
            all_entries,
            key=lambda e: (
                len(e.source_phrase),
                1 if e.verified else 0,
                e.usage_count,
                e.updated_at,
            ),
            reverse=True,
        )

        for entry in sorted_entries:
            src_phrase = entry.source_phrase.strip()
            if not src_phrase:
                continue

            # Check if source phrase exists in input source_text
            if src_phrase in source_text:
                normalized_key = src_phrase.casefold()

                # Rule: Glossary overrides Phrase Memory
                if normalized_key in glossary_keys:
                    continue

                # Avoid duplicate matches for same source_phrase
                if normalized_key in seen_phrases:
                    continue

                seen_phrases.add(normalized_key)
                matched.append(PhraseMemoryMatch(entry=entry))

        return matched

    def find_matches_for_page(
        self,
        dialogues: Iterable[dict],
        source_language: str,
        target_language: str,
        *,
        glossary: dict[str, str] | None = None,
        prefer_verified: bool = True,
    ) -> dict[str, str]:
        """Collect a unified terminology dict {source_phrase: target_phrase} for all bubbles on a page."""
        terminology: dict[str, str] = {}
        for item in dialogues:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            matches = self.find_matches_in_text(
                source_text=text,
                source_language=source_language,
                target_language=target_language,
                glossary=glossary,
                prefer_verified=prefer_verified,
            )
            for m in matches:
                if m.source_phrase not in terminology:
                    terminology[m.source_phrase] = m.target_phrase
        return terminology
