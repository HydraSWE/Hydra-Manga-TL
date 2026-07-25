from __future__ import annotations

import importlib
import json
import os
import re
import site
from pathlib import Path
from typing import Any

from .base import PageDialogue, PageTranslation
from .prompts import SYSTEM_PROMPT, build_page_prompt


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


def _coerce_int(value: Any, default: int) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled", "enable"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled", "disable"}:
        return False
    return default


def _coerce_quantization(value: Any, default: int | None = None) -> int | None:
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_")
        mapping = {
            "f16": 0,
            "q4_0": 2,
            "q4_1": 3,
            "q8_0": 7,
            "q5_0": 8,
            "q5_1": 9,
            "q2_k": 10,
            "q3_k_s": 11,
            "q3_k_m": 12,
            "q3_k_l": 13,
            "q4_k_s": 14,
            "q4_k_m": 15,
            "q5_k_s": 16,
            "q5_k_m": 17,
            "q6_k": 18,
        }
        if normalized in mapping:
            return mapping[normalized]
    return default


def _resolve_runtime_config(runtime_config: dict[str, Any] | None = None) -> dict[str, Any]:
    dotenv_values = _load_dotenv_values()
    env = {**dotenv_values, **os.environ}
    resolved = {
        "n_ctx": _coerce_int(env.get("QWEN_N_CTX") or env.get("LLAMA_N_CTX") or 2048, 2048),
        "n_batch": _coerce_int(env.get("QWEN_N_BATCH") or env.get("LLAMA_N_BATCH") or 64, 64),
        "n_ubatch": _coerce_int(env.get("QWEN_N_UBATCH") or env.get("LLAMA_N_UBATCH") or 32, 32),
        "n_gpu_layers": _coerce_int(env.get("QWEN_N_GPU_LAYERS") or env.get("LLAMA_N_GPU_LAYERS") or -1, -1),
        "flash_attn": _coerce_bool(env.get("QWEN_FLASH_ATTN") or env.get("LLAMA_FLASH_ATTN") or True, True),
        "offload_kqv": _coerce_bool(env.get("QWEN_OFFLOAD_KQV") or env.get("LLAMA_OFFLOAD_KQV") or True, True),
        "op_offload": _coerce_bool(env.get("QWEN_OP_OFFLOAD") or env.get("LLAMA_OP_OFFLOAD") or True, True),
        "type_k": _coerce_quantization(env.get("QWEN_TYPE_K") or env.get("LLAMA_TYPE_K") or "q4_0", 2),
        "type_v": _coerce_quantization(env.get("QWEN_TYPE_V") or env.get("LLAMA_TYPE_V") or "q4_0", 2),
        "n_threads": _coerce_int(env.get("QWEN_N_THREADS") or env.get("LLAMA_N_THREADS") or 6, 6),
        "n_threads_batch": _coerce_int(env.get("QWEN_N_THREADS_BATCH") or env.get("LLAMA_N_THREADS_BATCH") or 6, 6),
        "verbose": _coerce_bool(env.get("QWEN_VERBOSE") or env.get("LLAMA_VERBOSE") or False, False),
    }
    if runtime_config:
        resolved.update({key: value for key, value in runtime_config.items() if value is not None})
        if "type_k" in runtime_config and runtime_config["type_k"] is not None:
            resolved["type_k"] = _coerce_quantization(runtime_config["type_k"], resolved["type_k"])
        if "type_v" in runtime_config and runtime_config["type_v"] is not None:
            resolved["type_v"] = _coerce_quantization(runtime_config["type_v"], resolved["type_v"])
    return resolved


def _add_nvidia_dll_directories() -> None:
    if os.name != "nt":
        return
    candidates: list[Path] = []
    try:
        candidates.extend(Path(path) for path in site.getsitepackages())
    except Exception:
        pass
    candidates.append(Path(__file__).resolve().parents[2])

    for root in candidates:
        nvidia_root = root / "nvidia"
        if not nvidia_root.is_dir():
            continue
        for folder in nvidia_root.glob("*"):
            bin_dir = folder / "bin"
            if not bin_dir.is_dir():
                continue
            try:
                os.add_dll_directory(str(bin_dir))
            except (FileNotFoundError, OSError):
                pass
            os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ.get("PATH", "")


class QwenGGUFEngine:
    """Optional local Qwen GGUF engine backed by llama-cpp-python when available."""

    def __init__(
        self,
        *,
        model_path: str | None = None,
        glossary: dict[str, str] | None = None,
        model_name: str = "Qwen3-4B-Instruct-2507",
        temperature: float = 0.1,
        runtime_config: dict[str, Any] | None = None,
    ) -> None:
        self.model_path = model_path
        self.glossary = glossary or {}
        self.model_name = model_name
        self.temperature = temperature
        self.runtime_config = _resolve_runtime_config(runtime_config)
        self._loaded = False
        self._llama = None

    @property
    def engine_id(self) -> str:
        return f"qwen-gguf:{self.model_name}"

    def load(self) -> None:
        if self._loaded:
            return
        if not self.model_path:
            self._loaded = False
            raise FileNotFoundError("Qwen GGUF model not installed")
        model_path = Path(self.model_path)
        if not model_path.exists():
            self._loaded = False
            raise FileNotFoundError(f"Qwen GGUF model not found: {self.model_path}")
        _add_nvidia_dll_directories()
        try:
            llama_module = importlib.import_module("llama_cpp")
            llama_cls = getattr(llama_module, "Llama")
        except Exception as exc:  # pragma: no cover - optional dependency path
            raise RuntimeError("Optional dependency 'llama-cpp-python' is not installed; install it to enable local Qwen translation.") from exc
        self._llama = llama_cls(
            model_path=str(model_path),
            n_ctx=self.runtime_config["n_ctx"],
            n_batch=self.runtime_config["n_batch"],
            n_ubatch=self.runtime_config["n_ubatch"],
            n_gpu_layers=self.runtime_config["n_gpu_layers"],
            n_threads=self.runtime_config["n_threads"],
            n_threads_batch=self.runtime_config["n_threads_batch"],
            flash_attn=self.runtime_config["flash_attn"],
            offload_kqv=self.runtime_config["offload_kqv"],
            op_offload=self.runtime_config["op_offload"],
            type_k=self.runtime_config["type_k"],
            type_v=self.runtime_config["type_v"],
            verbose=self.runtime_config["verbose"],
        )
        self._loaded = True

    @staticmethod
    def _looks_like_name(text: str) -> bool:
        stripped = str(text).strip()
        if not stripped:
            return False
        if any(char in stripped for char in "!?.,;:()[]{}"):
            return False
        if len(stripped) > 6:
            return False
        if not any(("\u3400" <= char <= "\u9fff") for char in stripped):
            return False
        return True

    def protect_dialogue(self, dialogue: list[dict]) -> tuple[list[dict], dict[str, str]]:
        protected: list[dict] = []
        mapping: dict[str, str] = {}
        for index, item in enumerate(dialogue, 1):
            source_text = str(item.get("text", "")).strip()
            if self._looks_like_name(source_text):
                placeholder = f"<CHAR_{index:03d}>"
                mapping[placeholder] = source_text
                protected.append({**item, "text": placeholder})
            else:
                protected.append({**item, "text": source_text})
        return protected, mapping

    @staticmethod
    def restore_protected_text(text: str, mapping: dict[str, str]) -> str:
        restored = str(text)
        if not mapping:
            return restored
        for placeholder, original in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
            restored = restored.replace(placeholder, original)
        return restored

    def translate_page(self, page: PageDialogue) -> PageTranslation:
        if not self._loaded:
            self.load()
        if self._llama is None:
            raise RuntimeError("Qwen GGUF engine could not be loaded")
        if not page.dialogue:
            return PageTranslation(source_language=page.source_language, target_language=page.target_language, translations=[])
        dialog_items = [{"id": str(item.get("id", "")), "text": str(item.get("text", ""))} for item in page.dialogue]
        protected_dialogue, entity_map = self.protect_dialogue(dialog_items)
        prompt = build_page_prompt(
            source_language=page.source_language,
            target_language=page.target_language,
            style="Manga",
            glossary=self.glossary,
            dialogue=protected_dialogue,
            temperature=self.temperature,
            page_context=page.page_context,
            protected_entities=entity_map,
        )
        try:
            completion = self._llama.create_chat_completion(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=self.temperature,
                max_tokens=512,
            )
            raw = completion["choices"][0]["message"]["content"]
        except AttributeError:
            result = self._llama(prompt, max_tokens=512, temperature=self.temperature, echo=False)
            raw = result["choices"][0]["text"]
        except Exception as exc:  # pragma: no cover - runtime-specific
            raise RuntimeError(f"Qwen GGUF generation failed: {exc}") from exc
        payload = extract_first_json_object(raw)
        translations = payload.get("translations", [])
        if not isinstance(translations, list):
            raise RuntimeError("Qwen response did not include a translations array")
        expected_ids = [str(item.get("id", "")) for item in page.dialogue]
        by_id: dict[str, dict[str, str]] = {}
        for item in translations:
            if not isinstance(item, dict):
                continue
            entry_id = str(item.get("id", "")).strip()
            entry_text = self.restore_protected_text(str(item.get("text", "")).strip(), entity_map)
            if not entry_id:
                raise RuntimeError("Qwen response omitted translation ids")
            by_id[entry_id] = {"id": entry_id, "text": entry_text}
        if len(by_id) != len(expected_ids):
            raise RuntimeError("Qwen response did not preserve the expected number of ids")
        normalized = []
        for entry_id in expected_ids:
            if entry_id not in by_id:
                raise RuntimeError(f"Qwen response omitted id {entry_id}")
            normalized.append({"id": entry_id, "text": by_id[entry_id]["text"]})
        return PageTranslation(
            source_language=page.source_language,
            target_language=page.target_language,
            translations=normalized,
        )

    def unload(self) -> None:
        self._llama = None
        self._loaded = False


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def extract_first_json_object(raw: str) -> dict[str, Any]:
    """Best-effort extraction of the first top-level JSON object."""
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        return json.loads(raw)
    match = _JSON_RE.search(raw)
    if not match:
        raise ValueError("No JSON object found")
    return json.loads(match.group(0))
