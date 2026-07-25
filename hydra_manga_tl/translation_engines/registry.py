from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .base import TranslationEngine
from .marian_engine import MarianPageEngine
from .qwen_engine import QwenGGUFEngine
from .remote_engine import DeepSeekPageEngine, GeminiPageEngine, GooglePageEngine, GroqPageEngine


EngineFactory = Callable[..., TranslationEngine]


@dataclass(frozen=True)
class EngineRegistration:
    key: str
    label: str
    factory: EngineFactory
    cloud: bool = False


def _groq_factory(**kwargs: Any) -> TranslationEngine:
    return GroqPageEngine(
        glossary=kwargs.get("glossary"),
        model=kwargs.get("model"),
    )


def _google_factory(**kwargs: Any) -> TranslationEngine:
    return GooglePageEngine(glossary=kwargs.get("glossary"))


def _gemini_factory(**kwargs: Any) -> TranslationEngine:
    return GeminiPageEngine(
        glossary=kwargs.get("glossary"),
        model=kwargs.get("model"),
    )


def _deepseek_factory(**kwargs: Any) -> TranslationEngine:
    return DeepSeekPageEngine(
        glossary=kwargs.get("glossary"),
        model=kwargs.get("model"),
    )


def _marian_factory(**kwargs: Any) -> TranslationEngine:
    return MarianPageEngine(glossary=kwargs.get("glossary"))


def _qwen_factory(**kwargs: Any) -> TranslationEngine:
    return QwenGGUFEngine(
        model_path=kwargs.get("model_path") or kwargs.get("qwen_model_path"),
        glossary=kwargs.get("glossary"),
        model_name=kwargs.get("model_name") or "Qwen3-4B-Instruct-2507",
        runtime_config=kwargs.get("runtime_config"),
    )


TRANSLATION_PROVIDER_REGISTRY: dict[str, EngineRegistration] = {
    "groq": EngineRegistration("groq", "Groq", _groq_factory, cloud=True),
    "google": EngineRegistration("google", "Google Translate", _google_factory, cloud=True),
    "gemini": EngineRegistration("gemini", "Gemini", _gemini_factory, cloud=True),
    "deepseek": EngineRegistration("deepseek", "DeepSeek", _deepseek_factory, cloud=True),
    "marian": EngineRegistration("marian", "MarianMT", _marian_factory),
    "qwen": EngineRegistration("qwen", "Local Qwen GGUF", _qwen_factory),
}


def create_translation_engine(provider: str, **kwargs: Any) -> TranslationEngine:
    normalized = (provider or "qwen").strip().lower()
    registration = TRANSLATION_PROVIDER_REGISTRY.get(normalized)
    if registration is None:
        registration = TRANSLATION_PROVIDER_REGISTRY["marian"]
    return registration.factory(**kwargs)
