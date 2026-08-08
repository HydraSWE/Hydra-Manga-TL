from __future__ import annotations

import importlib
import json
import logging
import os
import site
from pathlib import Path
from typing import Any

from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.translation.postprocessing import clean_pronoun_artifacts
from .base import PageDialogue, PageTranslation, prepare_dialogue_item
from .prompts import SYSTEM_PROMPT, build_page_prompt


LOGGER = logging.getLogger(__name__)


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
        "n_ctx": _coerce_int(env.get("QWEN_N_CTX") or env.get("LLAMA_N_CTX") or 4096, 4096),
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
        "max_batch_items": _coerce_int(env.get("QWEN_MAX_BATCH_ITEMS") or 4, 4),
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
            LOGGER.info(
                "Qwen runtime config: %s",
                json.dumps(self.runtime_config, indent=2)
            )
            LOGGER.info("Model path: %s", model_path)
            LOGGER.info("llama_cpp version: %s", llama_module.__version__)
            llama_cls = getattr(llama_module, "Llama")
        except Exception as exc:  # pragma: no cover - optional dependency path
            raise RuntimeError(
                "Local Qwen runtime could not load llama-cpp-python or one of "
                f"its native dependencies: {exc}"
            ) from exc
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
        print("=" * 80)
        print("MODEL:", model_path)
        print("CONFIG:", self.runtime_config)
        print("=" * 80)

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
        if mapping:
            for placeholder, original in sorted(mapping.items(), key=lambda item: len(item[0]), reverse=True):
                restored = restored.replace(placeholder, original)
        return clean_pronoun_artifacts(restored)

    def translate_page(self, page: PageDialogue) -> PageTranslation:
        if not self._loaded:
            self.load()
        if self._llama is None:
            raise RuntimeError("Qwen GGUF engine could not be loaded")
        if not page.dialogue:
            return PageTranslation(source_language=page.source_language, target_language=page.target_language, translations=[])
        dialog_items = [prepare_dialogue_item(item) for item in page.dialogue]
        protected_dialogue, entity_map = self.protect_dialogue(dialog_items)
        pm_terms = None
        if getattr(SETTINGS, "phrase_memory_enabled", True):
            try:
                from hydra_manga_tl.translation.phrase_memory import PHRASE_MEMORY
                pm_terms = PHRASE_MEMORY.find_terminology_for_page(
                    page.dialogue,
                    page.source_language,
                    page.target_language,
                    glossary=self.glossary,
                    prefer_verified=getattr(SETTINGS, "phrase_memory_prefer_verified", True),
                )
            except Exception:
                pm_terms = None
        n_ctx = self.runtime_config["n_ctx"]
        # Reserve 200 tokens for ChatML framing overhead
        chatml_overhead = 200
        # Maximum retries for a single batch before giving up
        max_retries = 2
        max_batch_items = max(1, _coerce_int(self.runtime_config.get("max_batch_items"), 4))

        def _estimate_output_tokens(count: int) -> int:
            """Estimate output tokens needed: JSON wrapper + ~64 tokens per entry."""
            return 48 + count * 64

        def get_token_count(chunk: list[dict]) -> int:
            prompt = build_page_prompt(
                source_language=page.source_language,
                target_language=page.target_language,
                style="Manga",
                glossary=self.glossary,
                dialogue=chunk,
                temperature=self.temperature,
                page_context=page.page_context,
                protected_entities=entity_map,
                phrase_memory_terminology=pm_terms,
            )
            raw_text = SYSTEM_PROMPT + prompt
            if hasattr(self._llama, "tokenize"):
                return len(self._llama.tokenize(raw_text.encode("utf-8")))
            return len(raw_text) // 4

        def fits_in_context(chunk: list[dict]) -> bool:
            prompt_tokens = get_token_count(chunk)
            output_tokens = _estimate_output_tokens(len(chunk))
            total = prompt_tokens + output_tokens + chatml_overhead
            return total <= n_ctx

        def split_dialogue(chunk: list[dict]) -> list[list[dict]]:
            if not chunk:
                return []
            if len(chunk) <= max_batch_items and fits_in_context(chunk):
                return [chunk]
            if len(chunk) == 1:
                # Single item exceeds context — cannot split further, send anyway
                # and let the model do its best (it will likely truncate)
                LOGGER.warning(
                    "Single dialogue item exceeds context window (%d tokens); "
                    "sending anyway with best-effort generation",
                    get_token_count(chunk),
                )
                return [chunk]
            mid = len(chunk) // 2
            return split_dialogue(chunk[:mid]) + split_dialogue(chunk[mid:])

        batches = split_dialogue(protected_dialogue)
        if len(batches) > 1:
            LOGGER.info(
                "Auto token management: split %d dialogue items into %d batches "
                "(n_ctx=%d)",
                len(protected_dialogue), len(batches), n_ctx,
            )
        all_translations: list[dict[str, str]] = []

        def _translate_batch(batch: list[dict]) -> list[dict]:
            """Translate a single batch, returning normalized translation dicts."""
            batch_expected_ids = [str(item.get("id", "")) for item in batch]
            prompt = build_page_prompt(
                source_language=page.source_language,
                target_language=page.target_language,
                style="Manga",
                glossary=self.glossary,
                dialogue=batch,
                temperature=self.temperature,
                page_context=page.page_context,
                protected_entities=entity_map,
                phrase_memory_terminology=pm_terms,
            )
            prompt += (
                "\nStrict batch contract:\n"
                f"- Return exactly {len(batch_expected_ids)} translation object(s).\n"
                f"- Return these ids exactly once, in this exact order: {json.dumps(batch_expected_ids, ensure_ascii=False)}.\n"
                "- Do not include translations from any previous, later, or example batch.\n"
                "- If unsure, still return one object per listed id with the best faithful translation.\n"
            )

            batch_max_tokens = max(512, _estimate_output_tokens(len(batch)))
            # Clamp to remaining context after prompt
            prompt_tokens = get_token_count(batch)
            remaining = n_ctx - prompt_tokens - chatml_overhead
            if remaining > 0:
                batch_max_tokens = min(batch_max_tokens, remaining)

            try:
                completion = self._llama.create_chat_completion(
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    temperature=self.temperature,
                    max_tokens=batch_max_tokens,
                )
                raw = completion["choices"][0]["message"]["content"]
            except AttributeError:
                result = self._llama(prompt, max_tokens=batch_max_tokens, temperature=self.temperature, echo=False)
                raw = result["choices"][0]["text"]
            except ValueError as exc:
                # Token overflow — will be caught by retry logic
                raise
            except Exception as exc:  # pragma: no cover - runtime-specific
                raise RuntimeError(f"Qwen GGUF generation failed: {exc}") from exc

            payload = extract_first_json_object(raw)
            batch_translations = payload.get("translations", [])
            if not isinstance(batch_translations, list):
                raise RuntimeError("Qwen response did not include a translations array")

            # --- ID reconciliation ---
            by_id: dict[str, dict[str, str]] = {}
            usable_items: list[dict[str, str]] = []
            for item in batch_translations:
                if not isinstance(item, dict):
                    continue
                entry_id = str(item.get("id", "")).strip()
                entry_text = self.restore_protected_text(str(item.get("text", "")).strip(), entity_map)
                if entry_text:
                    usable_items.append({"id": entry_id, "text": entry_text})
                if not entry_id:
                    if len(batch_expected_ids) == 1 and len(batch_translations) == 1:
                        entry_id = batch_expected_ids[0]
                    else:
                        entry_id = ""
                by_id[entry_id] = {"id": entry_id, "text": entry_text}

            if len(batch_expected_ids) == 1 and len(by_id) == 1 and batch_expected_ids[0] not in by_id:
                only = next(iter(by_id.values()))
                by_id = {batch_expected_ids[0]: {"id": batch_expected_ids[0], "text": only["text"]}}

            # Check if all expected IDs are present by name
            missing_ids = [eid for eid in batch_expected_ids if eid not in by_id]
            result_items: list[dict[str, str]] = []
            if not missing_ids:
                # Perfect ID match — use ID-based mapping
                for entry_id in batch_expected_ids:
                    result_items.append({"id": entry_id, "text": by_id[entry_id]["text"]})
            elif len(batch_translations) == len(batch_expected_ids):
                # Count matches but IDs are mangled — fall back to positional mapping
                LOGGER.warning(
                    "Qwen returned mangled IDs for batch of %d items; using positional mapping",
                    len(batch_expected_ids),
                )
                for idx, entry_id in enumerate(batch_expected_ids):
                    item = batch_translations[idx]
                    entry_text = self.restore_protected_text(
                        str(item.get("text", "")).strip() if isinstance(item, dict) else "",
                        entity_map,
                    )
                    result_items.append({"id": entry_id, "text": entry_text})
            elif len(batch_expected_ids) == 1 and usable_items:
                LOGGER.warning(
                    "Qwen returned %d translations for single-item batch id=%s; "
                    "using first usable translation by position",
                    len(batch_translations),
                    batch_expected_ids[0],
                )
                result_items.append({"id": batch_expected_ids[0], "text": usable_items[0]["text"]})
            else:
                raise RuntimeError(
                    f"Qwen response returned {len(batch_translations)} translations "
                    f"but expected {len(batch_expected_ids)}"
                )
            return result_items

        # --- Process each batch with automatic retry and re-split ---
        pending_batches = list(batches)
        while pending_batches:
            batch = pending_batches.pop(0)
            retries = 0
            while True:
                try:
                    result_items = _translate_batch(batch)
                    all_translations.extend(result_items)
                    break
                except (RuntimeError, ValueError) as exc:
                    retries += 1
                    if len(batch) > 1 and retries <= max_retries:
                        # Re-split the failed batch in half and retry
                        mid = len(batch) // 2
                        LOGGER.warning(
                            "Batch of %d items failed (%s); re-splitting into %d + %d",
                            len(batch), exc, mid, len(batch) - mid,
                        )
                        # Insert the two halves at the front of pending
                        pending_batches.insert(0, batch[mid:])
                        pending_batches.insert(0, batch[:mid])
                        break
                    elif retries <= max_retries:
                        LOGGER.warning(
                            "Single-item batch failed (%s); retrying (%d/%d)",
                            exc, retries, max_retries,
                        )
                        continue
                    else:
                        raise

        return PageTranslation(
            source_language=page.source_language,
            target_language=page.target_language,
            translations=all_translations,
        )

    def unload(self) -> None:
        llama, self._llama = self._llama, None
        self._loaded = False
        if llama is None:
            return
        close = getattr(llama, "close", None)
        if not callable(close):
            return
        try:
            close()
        except Exception:
            LOGGER.exception("Qwen native context cleanup failed")


def extract_first_json_object(raw: str) -> dict[str, Any]:
    """Best-effort extraction of the first top-level JSON object."""
    decoder = json.JSONDecoder()
    source = str(raw).strip()
    for index, character in enumerate(source):
        if character != "{":
            continue
        try:
            payload, _end = decoder.raw_decode(source[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("No valid JSON object found")
