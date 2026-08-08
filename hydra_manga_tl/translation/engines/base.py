from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PageDialogue:
    """Stable representation of all bubbles on a page.

    `dialogue[i].id` must be unique within the page.
    """

    source_language: str
    target_language: str
    dialogue: list[dict]
    page_context: str | None = None


@dataclass(frozen=True)
class PageTranslation:
    """Validated translation output for a page."""

    source_language: str
    target_language: str
    translations: list[dict]


class TranslationEngine(Protocol):
    """Model-specific engine.

    Engines must provide:
    - load/unload for lifecycle management
    - translate_page with a *strictly structured* result.
    """

    engine_id: str

    def load(self) -> None: ...

    def translate_page(self, page: PageDialogue) -> PageTranslation: ...

    def unload(self) -> None: ...


def prepare_dialogue_item(item: dict) -> dict:
    """Format dialogue block for prompt injection, including available metadata."""
    payload = {
        "id": str(item.get("id", "")),
        "text": str(item.get("text", "")).strip(),
    }
    for field in (
        "source_direction",
        "confidence",
        "region_type",
        "reading_order",
        "ocr_review_reasons",
        "source_member_texts",
        "decorative_symbols",
        "preserved_marks",
    ):
        if field in item and item[field] is not None and item[field] != "":
            payload[field] = item[field]
    return payload
