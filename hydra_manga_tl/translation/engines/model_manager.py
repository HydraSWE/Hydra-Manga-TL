from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from hydra_manga_tl.core.normalization import normalize_page_translation
from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.translation.memory import (
    TRANSLATION_MEMORY,
    TranslationMemory,
    normalize_tm_source_text,
)
from .base import PageDialogue, PageTranslation, TranslationEngine
from .registry import TRANSLATION_PROVIDER_REGISTRY


LOGGER = logging.getLogger(__name__)


class TranslationValidationError(RuntimeError):
    pass


def _load_dotenv_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for candidate in [Path.cwd() / ".env", Path(__file__).resolve().parents[2] / ".env"]:
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = [part.strip() for part in stripped.split("=", 1)]
            if key and value and key not in values:
                values[key] = value.strip('"').strip("'")
    return values


@dataclass(frozen=True)
class ModelPackage:
    key: str
    label: str
    description: str
    filename: str
    estimated_size_gb: float
    quantization: str = "4-bit"
    recommended_for: str = "Balanced"

    @property
    def estimated_download(self) -> str:
        return f"{self.estimated_size_gb:.1f} GB"


KNOWN_MODEL_PACKAGES = {
    "qwen3-4b": ModelPackage(
        key="qwen3-4b",
        label="Qwen3 4B — Balanced",
        description="4-bit GGUF runtime for local page translation on mid-range hardware.",
        filename="qwen3-4b-instruct-2507-q4_k_m.gguf",
        estimated_size_gb=3.5,
        quantization="4-bit",
        recommended_for="Balanced",
    ),
    "qwen2.5-7b": ModelPackage(
        key="qwen2.5-7b",
        label="Qwen2.5 7B — Quality",
        description="Higher-quality 4-bit GGUF option for systems with more RAM/VRAM.",
        filename="qwen2.5-7b-instruct-q4_k_m.gguf",
        estimated_size_gb=4.8,
        quantization="4-bit",
        recommended_for="Quality",
    ),
}


def scan_local_qwen_models() -> list[ModelPackage]:
    """Scan candidate model folders for local .gguf files."""
    candidates = [
        Path.cwd() / "models",
        Path.cwd() / "models" / "qwen",
        Path(__file__).resolve().parents[3] / "models",
        Path(__file__).resolve().parents[3] / "models" / "qwen",
    ]
    seen_paths: set[Path] = set()
    packages: list[ModelPackage] = []

    for folder in candidates:
        if not folder.is_dir():
            continue
        for file in folder.glob("*.gguf"):
            resolved = file.resolve()
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            try:
                size_gb = round(resolved.stat().st_size / (1024 ** 3), 2)
            except OSError:
                size_gb = 0.0
            key = f"local:{resolved.name}"
            label = f"Local: {resolved.name} ({size_gb:.1f} GB)"
            packages.append(
                ModelPackage(
                    key=key,
                    label=label,
                    description=f"Local GGUF model found at {resolved}",
                    filename=str(resolved),
                    estimated_size_gb=size_gb,
                    quantization="Local",
                    recommended_for="Local File",
                )
            )
    return packages



@dataclass
class EngineSelection:
    literal_engine_id: str


class TranslationEngineManager:
    """Selects engines and guarantees a validated page translation.

    v0.8 behavior:
    - Construct only the selected engine
    - Validate output JSON shape
    - Lazily construct only the explicit fallback after a failure
    """

    def __init__(
        self,
        *,
        glossary: dict[str, str] | None = None,
        qwen_model_path: str | None = None,
        preferred_engine: str = "qwen",
        fallback_engine: str | None = "marian",
        qwen_model_name: str = "Qwen3-4B-Instruct-2507",
        provider_models: dict[str, str] | None = None,
        provider_base_urls: dict[str, str] | None = None,
        translation_memory: TranslationMemory | None = None,
        translation_memory_enabled: bool = True,
        translation_memory_prefer_verified: bool = True,
        runtime_config: dict[str, Any] | None = None,
        allow_local_fallback_for_cloud: bool = False,
    ) -> None:
        self.glossary = glossary or {}
        self.preferred_engine = (preferred_engine or "qwen").strip().lower()
        normalized_fallback = str(fallback_engine or "").strip().lower()
        self.fallback_engine = normalized_fallback if normalized_fallback in TRANSLATION_PROVIDER_REGISTRY else None
        if self.fallback_engine == self.preferred_engine:
            self.fallback_engine = None
        preferred_registration = TRANSLATION_PROVIDER_REGISTRY.get(self.preferred_engine)
        fallback_registration = TRANSLATION_PROVIDER_REGISTRY.get(self.fallback_engine or "")
        if (
            preferred_registration is not None
            and preferred_registration.cloud
            and fallback_registration is not None
            and not fallback_registration.cloud
            and not allow_local_fallback_for_cloud
        ):
            self.fallback_engine = None
        self.provider_models = provider_models or {}
        self.provider_base_urls = provider_base_urls or {}
        self.translation_memory = translation_memory or TRANSLATION_MEMORY
        self.translation_memory_enabled = bool(translation_memory_enabled)
        self.translation_memory_prefer_verified = bool(
            translation_memory_prefer_verified
        )
        self._failed_engines: set[str] = set()
        self.last_engine_id = ""
        dotenv_values = _load_dotenv_values()
        env_path = os.environ.get("QWEN_MODEL_PATH", "").strip() or dotenv_values.get("QWEN_MODEL_PATH", "").strip()
        resolved_qwen_model_path = qwen_model_path or env_path or None
        self._engine_kwargs = {
            "glossary": self.glossary,
            "model_path": resolved_qwen_model_path,
            "qwen_model_path": resolved_qwen_model_path,
            "model_name": qwen_model_name,
            "runtime_config": runtime_config,
        }
        self.engines: dict[str, TranslationEngine] = {}
        self._engine_lock = threading.RLock()
        self._resolved_qwen_model_path = resolved_qwen_model_path

    def _engine(self, key: str) -> TranslationEngine:
        with self._engine_lock:
            engine = self.engines.get(key)
            if engine is not None:
                return engine
            registration = TRANSLATION_PROVIDER_REGISTRY[key]
            kwargs = dict(self._engine_kwargs)
            if key in self.provider_models:
                kwargs["model"] = self.provider_models[key]
            if key in self.provider_base_urls:
                kwargs["base_url"] = self.provider_base_urls[key]
            if key == "qwen":
                kwargs["model_path"] = self._resolved_qwen_model_path
            engine = registration.factory(**kwargs)
            self.engines[key] = engine
            return engine

    @property
    def qwen(self) -> TranslationEngine:
        return self._engine("qwen")

    @property
    def marian(self) -> TranslationEngine:
        return self._engine("marian")

    def load(self) -> bool:
        selected = self._selected_engine_key()
        registration = TRANSLATION_PROVIDER_REGISTRY[selected]
        engine = self._engine(selected)
        if registration.cloud:
            return True
        try:
            engine.load()
            return True
        except Exception:
            self._failed_engines.add(selected)
            return False

    def unload(self) -> None:
        for engine in tuple(self.engines.values()):
            try:
                engine.unload()
            except Exception:
                pass

    @staticmethod
    def _validate_page_translation(page: PageDialogue, result: PageTranslation) -> None:
        input_ids = [str(item.get("id")) for item in page.dialogue]
        if len(set(input_ids)) != len(input_ids):
            raise TranslationValidationError("Duplicate ids in input")
        if not hasattr(result, "translations") or not isinstance(result.translations, list):
            raise TranslationValidationError("translations must be a list")

        out_ids = [str(item.get("id")) for item in result.translations]
        if len(out_ids) != len(input_ids):
            raise TranslationValidationError("translations length mismatch")
        if out_ids != input_ids:
            # order mismatch is also an error for predictable mapping
            raise TranslationValidationError("translation ids/order mismatch")
        for item in result.translations:
            text = str(item.get("text", "")).strip()
            if not text:
                raise TranslationValidationError("empty translation for id")

    @staticmethod
    def _is_single_manual_selection(page: PageDialogue) -> bool:
        if len(page.dialogue) != 1:
            return False
        entry_id = str(page.dialogue[0].get("id", "")).strip()
        if not entry_id.startswith("manual:"):
            return False
        context = str(page.page_context or "")
        return "Manual user-selected text box" in context

    @classmethod
    def _coerce_single_manual_translation(
        cls,
        page: PageDialogue,
        result: PageTranslation,
        *,
        engine_id: str,
    ) -> PageTranslation:
        if not cls._is_single_manual_selection(page):
            return result
        expected_id = str(page.dialogue[0].get("id", "")).strip()
        if (
            len(result.translations) == 1
            and str(result.translations[0].get("id", "")).strip() == expected_id
        ):
            return result
        usable = [
            dict(item)
            for item in result.translations
            if isinstance(item, dict) and str(item.get("text", "")).strip()
        ]
        if not usable:
            return result
        selected = usable[0]
        LOGGER.info(
            "Manual translation returned non-matching ids; using first usable text "
            "expected_id=%s returned_ids=%s returned_count=%d provider=%s",
            expected_id,
            [str(item.get("id", "")) for item in result.translations if isinstance(item, dict)],
            len(result.translations),
            engine_id,
        )
        recovered = {
            **selected,
            "id": expected_id,
            "text": str(selected.get("text", "")).strip(),
        }
        return PageTranslation(
            source_language=result.source_language,
            target_language=result.target_language,
            translations=[recovered],
        )

    def get_model_package(self, model_name: str | None = None) -> ModelPackage | None:
        if model_name is None:
            model_name = str(getattr(self.qwen, "model_name", "")).lower().replace("-", "")
        key = next((name for name in KNOWN_MODEL_PACKAGES if name in model_name.lower()), None)
        if key:
            return KNOWN_MODEL_PACKAGES.get(key)
        for pkg in scan_local_qwen_models():
            if pkg.key == model_name or pkg.filename == model_name or Path(pkg.filename).name == model_name:
                return pkg
        return None

    def translate_page(self, page: PageDialogue) -> PageTranslation:
        selected = self._selected_engine_key()
        selected_error: Exception | None = None
        if selected not in self._failed_engines:
            try:
                return self._translate_with_memory(self._engine(selected), page)
            except Exception as error:
                selected_error = error
                self._failed_engines.add(selected)
        if self.fallback_engine:
            try:
                return self._translate_with_memory(self._engine(self.fallback_engine), page)
            except Exception as fallback_error:
                if selected_error is None:
                    raise
                raise RuntimeError(
                    f"Translation engine '{selected}' failed: {selected_error}; "
                    f"fallback engine '{self.fallback_engine}' also failed: {fallback_error}"
                ) from fallback_error
        if selected_error is not None:
            raise RuntimeError(
                f"Translation engine '{selected}' failed: {selected_error}; no fallback is configured"
            ) from selected_error
        raise RuntimeError(f"Translation engine '{selected}' failed and no fallback is configured")

    def translate_page_using(self, engine_key: str, page: PageDialogue) -> tuple[PageTranslation, str]:
        """Translate with one explicit provider without changing fallback health."""
        key = str(engine_key or "").strip().lower()
        if key not in TRANSLATION_PROVIDER_REGISTRY:
            raise ValueError(f"Unknown translation engine: {key}")
        engine = self._engine(key)
        result = self._translate_with_memory(engine, page)
        return result, self._engine_identity(engine)

    def translate_cached_page(
        self,
        page: PageDialogue,
        cached: PageTranslation,
    ) -> PageTranslation:
        class CachedPageEngine:
            engine_id = "page-cache"

            def __init__(self, value: PageTranslation) -> None:
                self._by_id = {
                    str(item.get("id", "")): str(item.get("text", ""))
                    for item in value.translations
                }

            def load(self) -> None:
                return

            def unload(self) -> None:
                return

            def translate_page(self, requested: PageDialogue) -> PageTranslation:
                translations = []
                for item in requested.dialogue:
                    entry_id = str(item.get("id", ""))
                    text = self._by_id.get(entry_id, "")
                    if not text:
                        raise ValueError(
                            f"Page translation cache is missing {entry_id}"
                        )
                    translations.append({"id": entry_id, "text": text})
                return PageTranslation(
                    requested.source_language,
                    requested.target_language,
                    translations,
                )

        return self._translate_with_memory(CachedPageEngine(cached), page)

    def _selected_engine_key(self) -> str:
        if self.preferred_engine in TRANSLATION_PROVIDER_REGISTRY:
            return self.preferred_engine
        return "marian"

    def _engine_identity(self, engine: TranslationEngine) -> str:
        return str(getattr(engine, "engine_id", engine.__class__.__name__)).strip() or engine.__class__.__name__

    def _translate_with_memory(self, engine: TranslationEngine, page: PageDialogue) -> PageTranslation:
        engine_id = self._engine_identity(engine)
        if not page.dialogue:
            result = PageTranslation(page.source_language, page.target_language, [])
            self._validate_page_translation(page, result)
            return result

        translations_by_id: dict[str, dict[str, Any]] = {}
        missing_by_identity: dict[tuple[str, str], dict] = {}
        duplicate_ids: dict[tuple[str, str], list[str]] = {}
        matched_entry_ids: list[int] = []

        for item in page.dialogue:
            entry_id = str(item.get("id", "")).strip()
            source_text = str(item.get("text", "")).strip()
            region_type = normalize_region_type(
                item.get("region_type") or item.get("type")
            )
            match = (
                self.translation_memory.lookup(
                    engine_id=engine_id,
                    source_language=page.source_language,
                    target_language=page.target_language,
                    source_text=source_text,
                    region_type=region_type,
                    glossary=self.glossary,
                    prefer_verified=self.translation_memory_prefer_verified,
                    record_usage=False,
                )
                if self.translation_memory_enabled
                else None
            )
            if match is not None:
                if match.entry.id is not None:
                    matched_entry_ids.append(int(match.entry.id))
                translations_by_id[entry_id] = {
                    "id": entry_id,
                    "text": match.translated_text,
                    "translation_source": match.source,
                    "tm_match_type": match.match_type,
                    "tm_entry_id": match.entry.id,
                    "provider_id": match.entry.translation_provider,
                }
                continue
            identity = (normalize_tm_source_text(source_text), region_type)
            if identity in missing_by_identity:
                duplicate_ids.setdefault(identity, []).append(entry_id)
            else:
                missing_by_identity[identity] = item

        if missing_by_identity:
            self.last_engine_id = engine_id
            missing_page = PageDialogue(
                source_language=page.source_language,
                target_language=page.target_language,
                dialogue=[
                    {
                        key: value
                        for key, value in item.items()
                        if key not in {
                            "source_text_hash",
                            "source_region_hash",
                            "source_image",
                            "source_polygons",
                        }
                    }
                    for item in missing_by_identity.values()
                ],
                page_context=page.page_context,
            )
            result = engine.translate_page(missing_page)
            result = normalize_page_translation(missing_page, result)
            result = self._coerce_single_manual_translation(
                missing_page,
                result,
                engine_id=engine_id,
            )
            self._validate_page_translation(missing_page, result)
            source_by_id = {str(item.get("id", "")): str(item.get("text", "")).strip() for item in missing_page.dialogue}
            for translated in result.translations:
                entry_id = str(translated.get("id", "")).strip()
                translated_text = str(translated.get("text", "")).strip()
                source_text = source_by_id.get(entry_id, "")
                source_item = next(
                    item for item in missing_page.dialogue
                    if str(item.get("id", "")).strip() == entry_id
                )
                identity = (
                    normalize_tm_source_text(source_text),
                    normalize_region_type(
                        source_item.get("region_type") or source_item.get("type")
                    ),
                )
                resolved = {
                    "id": entry_id,
                    "text": translated_text,
                    "translation_source": "provider",
                    "tm_match_type": "",
                    "tm_entry_id": None,
                    "provider_id": engine_id,
                }
                translations_by_id[entry_id] = resolved
                for duplicate_id in duplicate_ids.get(identity, []):
                    translations_by_id[duplicate_id] = {
                        **resolved,
                        "id": duplicate_id,
                    }
        else:
            sources = {
                str(item.get("translation_source", ""))
                for item in translations_by_id.values()
            }
            self.last_engine_id = (
                "translation-memory"
                if sources == {"translation-memory"}
                else "legacy-cache"
            )
            if self.translation_memory_enabled and translations_by_id:
                self.translation_memory.record_provider_call_saved()

        final = PageTranslation(
            source_language=page.source_language,
            target_language=page.target_language,
            translations=[
                translations_by_id.get(
                    str(item.get("id", "")).strip(),
                    {"id": str(item.get("id", "")), "text": ""},
                )
                for item in page.dialogue
            ],
        )
        self._validate_page_translation(page, final)
        if matched_entry_ids:
            self.translation_memory.record_entry_hits(matched_entry_ids)
        return final
