"""Cache and Translation Memory helpers for SmartTranslationScheduler."""

from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
from typing import Any

from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.translation.memory import TRANSLATION_MEMORY
from hydra_manga_tl.translation.engines import PageTranslation
from hydra_manga_tl.translation.scheduler_models import (
    BubbleTranslationUnit,
    SmartPageJob,
    TranslationQueueItem,
)


class SmartSchedulerCacheMixin:
    def _store_dispatch_result(
        self,
        batch: list[TranslationQueueItem],
        result: PageTranslation,
        provider_id: str,
    ) -> None:
        by_id = {
            str(item.get("id", "")): str(item.get("text", ""))
            for item in result.translations
        }
        for item in batch:
            unit = item.unit
            self.store.set(
                unit,
                by_id.get(unit.bubble_id, ""),
                provider_id=provider_id,
                translation_source="provider",
            )

    def _hydrate_cached_or_tm(
        self,
        job: SmartPageJob,
        unit: BubbleTranslationUnit,
    ) -> bool:
        cached = self._read_bubble_cache(job, unit)
        if cached is not None:
            self.store.set(
                unit,
                str(cached.get("text", "")),
                provider_id=str(cached.get("provider_id", "bubble-cache")),
                translation_source=str(cached.get("translation_source", "bubble-cache")),
                tm_match_type=str(cached.get("tm_match_type", "")),
                tm_entry_id=cached.get("tm_entry_id"),
            )
            return True
        match = (
            TRANSLATION_MEMORY.lookup(
                source_text=unit.source_text,
                source_language=unit.source_language,
                target_language=unit.target_language,
                region_type=unit.region_type,
                prefer_verified=self.prefer_verified_tm,
                engine_id=self.primary_provider,
                glossary=self.glossary,
                record_usage=False,
            )
            if self.translation_memory_enabled
            else None
        )
        if match is None:
            return False
        self.store.set(
            unit,
            match.translated_text,
            provider_id=match.entry.translation_provider,
            translation_source=match.source,
            tm_match_type=match.match_type,
            tm_entry_id=match.entry.id,
        )
        return True

    def _hydrate_legacy_page_cache(self, job: SmartPageJob) -> bool:
        try:
            payload = json.loads(job.cache_path.read_text(encoding="utf-8"))
            cached = PageTranslation(
                source_language=str(payload.get("source_language", job.page.source_language)),
                target_language=str(payload.get("target_language", job.page.target_language)),
                translations=list(payload.get("translations", [])),
            )
        except (OSError, TypeError, ValueError):
            return False
        by_id = {
            str(item.get("id", "")): dict(item)
            for item in cached.translations
        }
        if not by_id:
            return False
        units = self._units_for_job(job)
        for unit in units:
            cached_item = by_id.get(unit.legacy_id)
            if not cached_item or not str(cached_item.get("text", "")).strip():
                return False
        for unit in units:
            cached_item = by_id[unit.legacy_id]
            self.store.set(
                unit,
                str(cached_item.get("text", "")),
                provider_id=str(cached_item.get("provider_id", "page-cache")),
                translation_source=str(cached_item.get("translation_source", "page-cache")),
                tm_match_type=str(cached_item.get("tm_match_type", "")),
                tm_entry_id=cached_item.get("tm_entry_id"),
            )
        return True

    def _units_for_job(self, job: SmartPageJob) -> list[BubbleTranslationUnit]:
        units: list[BubbleTranslationUnit] = []
        for index, item in enumerate(job.page.dialogue):
            legacy_id = str(item.get("id", f"r{index + 1}"))
            bubble_id = str(item.get("bubble_id") or legacy_id)
            units.append(BubbleTranslationUnit(
                bubble_id=bubble_id,
                legacy_id=legacy_id,
                page_index=job.page_index,
                page_number=job.page_index + 1,
                image_id=job.image_id,
                request_id=job.request_id,
                source_language=job.page.source_language,
                target_language=job.page.target_language,
                source_text=str(item.get("text", "")),
                region_type=normalize_region_type(item.get("region_type") or item.get("type")),
                page_context=str(job.page.page_context or ""),
                bubble_cache_dir=job.bubble_cache_dir,
                display_id=str(item.get("display_id", "")),
                bbox=list(item.get("bbox", [])),
                source_text_hash=str(item.get("source_text_hash", "")),
                source_region_hash=item.get("source_region_hash"),
            ))
        return units

    def _bubble_cache_path(self, unit: BubbleTranslationUnit) -> Path:
        payload = {
            "kind": "bubble-translation-v1",
            "bubble_id": unit.bubble_id,
            "source_language": unit.source_language,
            "target_language": unit.target_language,
            "source_text": unit.source_text,
            "region_type": unit.region_type,
            "page_context": unit.page_context,
            "provider": self.primary_provider,
            "glossary": self.glossary,
        }
        digest = hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        cache_dir = Path(unit.bubble_cache_dir or ".")
        return cache_dir / f"{unit.legacy_id}_{digest[:16]}.json"

    def _read_bubble_cache(
        self,
        job: SmartPageJob,
        unit: BubbleTranslationUnit,
    ) -> dict[str, Any] | None:
        path = self._bubble_cache_path(unit)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            return None
        text = str(payload.get("text", "")).strip()
        return payload if text else None

    def _write_bubble_cache(
        self,
        unit: BubbleTranslationUnit,
        provider_id: str,
    ) -> None:
        value = self.store._results.get(unit.bubble_id)
        if not value:
            return
        path = self._bubble_cache_path(unit)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({
                "kind": "bubble-translation-v1",
                "bubble_id": unit.bubble_id,
                "legacy_id": unit.legacy_id,
                "display_id": unit.display_id,
                "source_text": unit.source_text,
                "text": value.get("text", ""),
                "provider_id": provider_id,
                "translation_source": value.get("translation_source", "provider"),
                "tm_match_type": value.get("tm_match_type", ""),
                "tm_entry_id": value.get("tm_entry_id"),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(path)

    @staticmethod
    def _write_legacy_page_cache(job: SmartPageJob, translation: PageTranslation) -> None:
        job.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = job.cache_path.with_suffix(job.cache_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(asdict(translation), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(job.cache_path)
