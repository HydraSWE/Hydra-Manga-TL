"""Stable cache-key and JSON persistence facade for translation requests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from hydra_manga_tl.translation.engines import PageDialogue


def _json_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class TranslationCacheStore:
    """Normalize cache identities while allowing separate backing directories."""

    @staticmethod
    def file_digest(path: Path) -> str:
        digest = hashlib.sha256()
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def manual_selection_key(
        cls,
        source: Path,
        rect: list[int] | tuple[int, int, int, int],
        preferred_language: str,
        quality: str,
    ) -> str:
        return _json_hash({
            "kind": "manual-selection-ocr-v3-padded-pass",
            "source_sha256": cls.file_digest(source),
            "rect": [int(value) for value in rect],
            "preferred_language": str(preferred_language),
            "quality": str(quality),
        })

    @staticmethod
    def page_translation_key(
        page: PageDialogue,
        config: dict[str, Any],
        target: str,
    ) -> str:
        engine = str(config.get("translation_engine", "qwen") or "qwen").strip().lower()
        provider_models = dict(config.get("provider_models", {}) or {})
        provider_base_urls = dict(config.get("provider_base_urls", {}) or {})
        return _json_hash({
            "kind": "page-translation-v1",
            "source_language": page.source_language,
            "target_language": target,
            "dialogue": page.dialogue,
            "page_context": page.page_context,
            "glossary": config.get("glossary", {}),
            "engine": engine,
            "provider_model": provider_models.get(engine, ""),
            "provider_base_url": provider_base_urls.get(engine, ""),
            "qwen_model": (
                config.get("qwen_model_path") or config.get("qwen_model") or config.get("qwen_model_name", "")
                if engine == "qwen"
                else ""
            ),
            "localization_style": config.get("localization_style", "Manga"),
        })

    @staticmethod
    def read_json(path: Path) -> dict[str, Any] | None:
        try:
            value = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def write_json(path: Path, payload: dict[str, Any]) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


TRANSLATION_CACHE = TranslationCacheStore()
