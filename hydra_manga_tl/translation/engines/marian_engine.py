from __future__ import annotations

from dataclasses import dataclass

from .base import PageDialogue, PageTranslation, TranslationEngine


class MarianPageEngine:
    """MarianMT adapter as a PageTranslation engine.

    Initial milestone implementation keeps behavior simple:
    - translate each bubble independently (no page-level context)
    - preserve ordering and ids

    MarianMT itself is already implemented in `hydra_manga_tl.translation`.
    """

    engine_id = "marian"

    def __init__(self, *, batch_size: int = 8, glossary: dict[str, str] | None = None) -> None:
        from hydra_manga_tl.translation.service import MarianTranslator

        self._engine = MarianTranslator(batch_size=batch_size, glossary=glossary)

    def load(self) -> None:
        # Lazy-loading happens inside MarianTranslator on first translate.
        return

    def translate_page(self, page: PageDialogue) -> PageTranslation:
        dialogue = page.dialogue
        texts = [str(item.get("text", "")).strip() for item in dialogue]
        translated = self._engine.translate(texts, page.source_language, page.target_language)
        out = [
            {"id": str(item.get("id")), "text": translated[i]}
            for i, item in enumerate(dialogue)
        ]
        return PageTranslation(
            source_language=page.source_language,
            target_language=page.target_language,
            translations=out,
        )

    def unload(self) -> None:
        # MarianTranslator currently does not implement unload.
        return


# Backwards-friendly alias
TranslationEngine = MarianPageEngine  # type: ignore

