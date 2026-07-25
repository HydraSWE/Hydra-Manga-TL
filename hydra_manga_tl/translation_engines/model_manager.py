from __future__ import annotations

import json
import os
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from ..normalization import normalize_page_translation
from .base import PageDialogue, PageTranslation, TranslationEngine
from .registry import TRANSLATION_PROVIDER_REGISTRY
from .translation_memory import TranslationMemory


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
        translation_memory: TranslationMemory | None = None,
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
        self.translation_memory = translation_memory or TranslationMemory()
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
        self._resolved_qwen_model_path = resolved_qwen_model_path
        self._engine(self._selected_engine_key())

    def _engine(self, key: str) -> TranslationEngine:
        engine = self.engines.get(key)
        if engine is not None:
            return engine
        registration = TRANSLATION_PROVIDER_REGISTRY[key]
        kwargs = dict(self._engine_kwargs)
        if key in self.provider_models:
            kwargs["model"] = self.provider_models[key]
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

    def load(self) -> None:
        selected = self._selected_engine_key()
        registration = TRANSLATION_PROVIDER_REGISTRY[selected]
        if not registration.cloud:
            try:
                self._engine(selected).load()
            except Exception:
                self._failed_engines.add(selected)

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

    def get_model_package(self, model_name: str | None = None) -> ModelPackage | None:
        if model_name is None:
            model_name = str(getattr(self.qwen, "model_name", "")).lower().replace("-", "")
        key = next((name for name in KNOWN_MODEL_PACKAGES if name in model_name.lower()), None)
        return KNOWN_MODEL_PACKAGES.get(key) if key else None

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

    def _selected_engine_key(self) -> str:
        if self.preferred_engine in TRANSLATION_PROVIDER_REGISTRY:
            return self.preferred_engine
        return "marian"

    def _engine_identity(self, engine: TranslationEngine) -> str:
        return str(getattr(engine, "engine_id", engine.__class__.__name__)).strip() or engine.__class__.__name__

    def _translate_with_memory(self, engine: TranslationEngine, page: PageDialogue) -> PageTranslation:
        self.last_engine_id = self._engine_identity(engine)
        if not page.dialogue:
            result = PageTranslation(page.source_language, page.target_language, [])
            self._validate_page_translation(page, result)
            return result

        engine_id = self.last_engine_id
        translations_by_id: dict[str, str] = {}
        missing_by_text: dict[str, dict] = {}
        duplicate_ids: dict[str, list[str]] = {}

        for item in page.dialogue:
            entry_id = str(item.get("id", "")).strip()
            source_text = str(item.get("text", "")).strip()
            cached = self.translation_memory.get(
                engine_id=engine_id,
                source_language=page.source_language,
                target_language=page.target_language,
                source_text=source_text,
                glossary=self.glossary,
            )
            if cached is not None:
                translations_by_id[entry_id] = cached
                continue
            if source_text in missing_by_text:
                duplicate_ids.setdefault(source_text, []).append(entry_id)
            else:
                missing_by_text[source_text] = item

        if missing_by_text:
            missing_page = PageDialogue(
                source_language=page.source_language,
                target_language=page.target_language,
                dialogue=list(missing_by_text.values()),
                page_context=page.page_context,
            )
            result = engine.translate_page(missing_page)
            result = normalize_page_translation(missing_page, result)
            self._validate_page_translation(missing_page, result)
            source_by_id = {str(item.get("id", "")): str(item.get("text", "")).strip() for item in missing_page.dialogue}
            for translated in result.translations:
                entry_id = str(translated.get("id", "")).strip()
                translated_text = str(translated.get("text", "")).strip()
                source_text = source_by_id.get(entry_id, "")
                translations_by_id[entry_id] = translated_text
                self.translation_memory.put(
                    engine_id=engine_id,
                    source_language=page.source_language,
                    target_language=page.target_language,
                    source_text=source_text,
                    translated_text=translated_text,
                    glossary=self.glossary,
                )
                for duplicate_id in duplicate_ids.get(source_text, []):
                    translations_by_id[duplicate_id] = translated_text

        final = PageTranslation(
            source_language=page.source_language,
            target_language=page.target_language,
            translations=[
                {"id": str(item.get("id", "")), "text": translations_by_id.get(str(item.get("id", "")).strip(), "")}
                for item in page.dialogue
            ],
        )
        self._validate_page_translation(page, final)
        return final
