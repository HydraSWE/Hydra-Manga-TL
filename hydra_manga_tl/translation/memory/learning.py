"""Post-QA learning helpers shared by batch and manual translation paths."""

from __future__ import annotations

from typing import Iterable

from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.translation.engines.base import PageDialogue, PageTranslation

from hydra_manga_tl.core.settings import SETTINGS
from .database import TRANSLATION_MEMORY, TranslationMemory


def _provider_parts(identity: str) -> tuple[str, str]:
    value = str(identity or "").strip()
    if ":" not in value:
        return value, ""
    provider, model = value.split(":", 1)
    return provider, model


def learn_validated_page(
    page: PageDialogue,
    result: PageTranslation,
    *,
    valid_ids: Iterable[str] | None = None,
    memory: TranslationMemory = TRANSLATION_MEMORY,
    phrase_memory: object | None = None,
    project_id: str | None = None,
    series_id: str | None = None,
    quality_score: float = 1.0,
) -> int:
    """Learn only caller-approved units after translation and render QA."""
    if phrase_memory is None:
        from hydra_manga_tl.translation.phrase_memory import PHRASE_MEMORY
        phrase_memory = PHRASE_MEMORY


    allowed = {str(value) for value in valid_ids} if valid_ids is not None else None
    sources = {
        str(item.get("id", "")): item
        for item in page.dialogue
    }
    learned = 0
    for translated in result.translations:
        entry_id = str(translated.get("id", ""))
        if allowed is not None and entry_id not in allowed:
            continue
        if translated.get("translation_source") == "translation-memory":
            continue
        source = sources.get(entry_id)
        if source is None:
            continue
        provider, model = _provider_parts(
            str(translated.get("provider_id", ""))
        )
        entry = memory.record(
            source_text=str(source.get("text", "")),
            translated_text=str(translated.get("text", "")),
            source_language=page.source_language,
            target_language=page.target_language,
            region_type=normalize_region_type(
                source.get("region_type") or source.get("type")
            ),
            source_region_hash=source.get("source_region_hash") or None,
            translation_provider=provider,
            provider_model=model,
            verified=False,
            user_edited=False,
            quality_score=quality_score,
            origin="provider",
            project_id=project_id,
            series_id=series_id,
        )
        learned += int(entry is not None)

    if getattr(SETTINGS, "phrase_memory_auto_learn", True):
        try:
            phrase_memory.learn_page_phrases(
                dialogues=page.dialogue,
                translations=result.translations,
                source_language=page.source_language,
                target_language=page.target_language,
                valid_ids=valid_ids,
                project_id=project_id,
                series_id=series_id,
                origin="QA",
            )
        except Exception:
            pass

    return learned


def record_user_edited_translation(
    *,
    source_text: str,
    translated_text: str,
    source_language: str,
    target_language: str,
    region_type: str = "dialogue",
    memory: TranslationMemory = TRANSLATION_MEMORY,
    phrase_memory: object | None = None,
    project_id: str | None = None,
    series_id: str | None = None,
) -> None:
    """Record user-edited translation in both Translation Memory and Phrase Memory."""
    if phrase_memory is None:
        from hydra_manga_tl.translation.phrase_memory import PHRASE_MEMORY
        phrase_memory = PHRASE_MEMORY

    if getattr(SETTINGS, "translation_memory_store_user_edits", True):
        try:
            memory.record_user_edit(
                source_text=source_text,
                translated_text=translated_text,
                source_language=source_language,
                target_language=target_language,
                region_type=region_type,
                project_id=project_id,
                series_id=series_id,
            )
        except Exception:
            pass
    if getattr(SETTINGS, "phrase_memory_store_user_edits", True):
        try:
            phrase_memory.record_user_edit(
                source_phrase=source_text,
                target_phrase=translated_text,
                source_language=source_language,
                target_language=target_language,
                project_id=project_id,
                series_id=series_id,
            )
        except Exception:
            pass


