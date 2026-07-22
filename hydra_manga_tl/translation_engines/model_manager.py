from __future__ import annotations

import json
import os
from pathlib import Path
from dataclasses import dataclass
from typing import Any

from ..normalization import normalize_page_translation
from .base import PageDialogue, PageTranslation
from .marian_engine import MarianPageEngine
from .qwen_engine import QwenGGUFEngine, extract_first_json_object


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

    Milestone v0.6.0 behavior:
    - Try Qwen GGUF (if configured)
    - Validate output JSON shape
    - Fallback to Marian on any failure
    """

    def __init__(
        self,
        *,
        glossary: dict[str, str] | None = None,
        qwen_model_path: str | None = None,
        preferred_engine: str = "qwen",
        qwen_model_name: str = "Qwen3-4B-Instruct-2507",
        runtime_config: dict[str, Any] | None = None,
    ) -> None:
        self.glossary = glossary or {}
        self.preferred_engine = preferred_engine or "qwen"
        dotenv_values = _load_dotenv_values()
        env_path = os.environ.get("QWEN_MODEL_PATH", "").strip() or dotenv_values.get("QWEN_MODEL_PATH", "").strip()
        resolved_qwen_model_path = qwen_model_path or env_path or None
        self.qwen = QwenGGUFEngine(
            model_path=resolved_qwen_model_path,
            glossary=self.glossary,
            model_name=qwen_model_name,
            runtime_config=runtime_config,
        )
        self.marian = MarianPageEngine(glossary=self.glossary)

    def load(self) -> None:
        # Qwen is optional.
        try:
            self.qwen.load()
        except Exception:
            pass
        self.marian.load()

    def unload(self) -> None:
        try:
            self.qwen.unload()
        except Exception:
            pass
        try:
            self.marian.unload()
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
            model_name = self.qwen.model_name.lower().replace("-", "")
        key = next((name for name in KNOWN_MODEL_PACKAGES if name in model_name.lower()), None)
        return KNOWN_MODEL_PACKAGES.get(key) if key else None

    def translate_page(self, page: PageDialogue) -> PageTranslation:
        if self.preferred_engine == "marian":
            marian_result = self.marian.translate_page(page)
            marian_result = normalize_page_translation(page, marian_result)
            self._validate_page_translation(page, marian_result)
            return marian_result

        qwen_error: Exception | None = None
        try:
            self.qwen.load()
            qwen_result = self.qwen.translate_page(page)
            qwen_result = normalize_page_translation(page, qwen_result)
            self._validate_page_translation(page, qwen_result)
            return qwen_result
        except Exception as exc:
            qwen_error = exc

        marian_result = self.marian.translate_page(page)
        marian_result = normalize_page_translation(page, marian_result)
        self._validate_page_translation(page, marian_result)
        if qwen_error:
            # Swallow but keep deterministic behavior.
            pass
        return marian_result
