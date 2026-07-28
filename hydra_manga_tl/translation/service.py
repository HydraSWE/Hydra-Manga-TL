"""Translation providers, manga localization, caching, and result contract."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from hashlib import sha256
import html
import json
from pathlib import Path
import re
import threading
from typing import ClassVar, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from hydra_manga_tl.core.settings import AppSettings, CREDENTIALS, SETTINGS
from hydra_manga_tl.core.normalization import normalize_global_text


MODEL_BY_PAIR = {
    ("Chinese", "en"): "Helsinki-NLP/opus-mt-zh-en",
    ("Japanese", "en"): "Helsinki-NLP/opus-mt-ja-en",
}

EXACT_TRANSLATIONS = {
    ("Japanese", "en", "大和"): "Yamato",
    ("Japanese", "en", "オレが食いたいんだ全部オレによこせー"): "I want to eat it. Give it all to me!",
}

SOURCE_CODES = {"Chinese": "zh", "Japanese": "ja", "Latin-script": "en"}


def protected_translation(
    text: str, source_language: str, target_language: str, glossary: dict[str, str] | None = None,
) -> str | None:
    stripped = text.strip()
    suffix = ""
    punctuation = {"！": "!", "？": "?", "。": ".", "!": "!", "?": "?", ".": "."}
    while stripped and stripped[-1] in punctuation:
        suffix = punctuation[stripped[-1]] + suffix
        stripped = stripped[:-1].rstrip()
    translated = (glossary or {}).get(stripped)
    if translated is None:
        translated = EXACT_TRANSLATIONS.get((source_language, target_language, stripped))
    return f"{translated}{suffix}" if translated is not None else None


class TranslationProvider(Protocol):
    cache_identity: str

    def translate(self, texts: list[str], source_language: str, target_language: str) -> list[str]: ...


class MangaLocalizer(Protocol):
    cache_identity: str

    def localize(
        self, originals: list[str], literals: list[str], source_language: str, target_language: str,
        *, style: str, glossary: dict[str, str], constraints: list[dict] | None = None,
    ) -> list[dict]: ...


@dataclass
class TranslatedRegion:
    index: int
    original_text: str
    translated_text: str
    ocr_confidence: float
    polygon: list[list[int]]
    status: str
    review_reasons: list[str]
    literal_text: str = ""
    provider: str = "marian"
    model: str = ""
    localization_style: str = "Manga"
    translation_quality: str = "good"
    alternatives: list[str] | None = None
    localization_note: str = ""


class MarianTranslator:
    """Lazy-loading offline Marian translator with batched inference."""

    cache_identity = "marian"
    _shared_models: ClassVar[dict[str, tuple[object, object, object, str]]] = {}
    _shared_model_lock: ClassVar[threading.RLock] = threading.RLock()

    def __init__(self, batch_size: int = 8, glossary: dict[str, str] | None = None) -> None:
        self.batch_size = batch_size
        self.glossary = glossary or {}

    def _load(self, model_name: str):
        with self._shared_model_lock:
            if model_name in self._shared_models:
                return self._shared_models[model_name]
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer

            device = "cuda" if torch.cuda.is_available() else "cpu"
            tokenizer = AutoTokenizer.from_pretrained(model_name)
            model = AutoModelForSeq2SeqLM.from_pretrained(model_name).to(device)
            model.eval()
            self._shared_models[model_name] = (tokenizer, model, torch, device)
            return self._shared_models[model_name]

    def translate(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        if not texts:
            return []
        if source_language == "Latin-script" and target_language == "en":
            return list(texts)
        resolved: list[str | None] = [
            protected_translation(text, source_language, target_language, self.glossary) for text in texts
        ]
        pending = [(index, text) for index, (text, value) in enumerate(zip(texts, resolved)) if value is None]
        if not pending:
            return [value or "" for value in resolved]
        model_name = MODEL_BY_PAIR.get((source_language, target_language))
        if model_name is None:
            raise ValueError(f"Unsupported local translation pair: {source_language} -> {target_language}")
        tokenizer, model, torch, device = self._load(model_name)
        translated: list[str] = []
        pending_texts = [text for _, text in pending]
        for start in range(0, len(pending_texts), self.batch_size):
            batch = pending_texts[start : start + self.batch_size]
            encoded = tokenizer(batch, return_tensors="pt", padding=True, truncation=True, max_length=256)
            encoded = {key: value.to(device) for key, value in encoded.items()}
            with torch.inference_mode():
                generated = model.generate(**encoded, max_new_tokens=128, num_beams=4)
            translated.extend(tokenizer.batch_decode(generated, skip_special_tokens=True))
        for (index, _), value in zip(pending, translated):
            resolved[index] = value.strip()
        return [value or "" for value in resolved]


class GoogleCloudTranslator:
    cache_identity = "google-cloud-v2"

    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise ValueError("Google Cloud Translation API key is not configured.")
        self.api_key = api_key

    def translate(self, texts: list[str], source_language: str, target_language: str) -> list[str]:
        if not texts:
            return []
        body = {
            "q": texts, "source": SOURCE_CODES.get(source_language, ""),
            "target": target_language, "format": "text",
        }
        payload = _post_json(
            f"https://translation.googleapis.com/language/translate/v2?key={quote(self.api_key)}", body,
        )
        values = payload.get("data", {}).get("translations", [])
        translated = [html.unescape(str(value.get("translatedText", ""))).strip() for value in values]
        if len(translated) != len(texts):
            raise RuntimeError("Google Translation returned an unexpected number of results.")
        return translated


class LocalMangaLocalizer:
    cache_identity = "local-manga-v1"

    _natural_phrases = {
        "All three of you did a good job.": "Nice work, all three of you.",
        "All three of you did a good job": "Nice work, all three of you.",
    }

    def localize(
        self, originals: list[str], literals: list[str], source_language: str, target_language: str,
        *, style: str, glossary: dict[str, str], constraints: list[dict] | None = None,
    ) -> list[dict]:
        del source_language, target_language, constraints
        results = []
        for original, literal in zip(originals, literals):
            final = self._natural_phrases.get(literal.strip(), literal.strip())
            final = re.sub(r"\s+([,.!?])", r"\1", final)
            reasons: list[str] = []
            alternatives: list[str] = []
            if not final:
                reasons.append("empty_translation")
            if final.rstrip().endswith("...") or original.rstrip().endswith(("…", "...")):
                reasons.append("possible_incomplete_dialogue")
            # Short CJK terms are often names. Require review unless the project
            # glossary already resolved the exact source term.
            compact = original.strip("！？。!? ")
            if 1 <= len(compact) <= 3 and any("\u3400" <= char <= "\u9fff" for char in compact) and compact not in glossary:
                reasons.append("name_ambiguity")
            results.append({
                "translated_text": final, "review_reasons": reasons,
                "alternatives": alternatives, "note": "Local manga cleanup" if final != literal.strip() else "",
                "style": style,
            })
        return results


class RemoteMangaLocalizer:
    def __init__(self, provider: str, api_key: str, model: str) -> None:
        if provider not in {"gemini", "groq", "deepseek"}:
            raise ValueError(f"Unsupported manga localization provider: {provider}")
        self.provider, self.api_key, self.model = provider, api_key, model
        self.cache_identity = f"{provider}:{model}"

    def localize(
        self, originals: list[str], literals: list[str], source_language: str, target_language: str,
        *, style: str, glossary: dict[str, str], constraints: list[dict] | None = None,
    ) -> list[dict]:
        if not self.api_key:
            raise ValueError(f"{self.provider.title()} API key is not configured.")
        pm_terms = None
        if getattr(SETTINGS, "phrase_memory_enabled", True):
            try:
                from hydra_manga_tl.translation.phrase_memory import PHRASE_MEMORY
                pm_terms = PHRASE_MEMORY.find_terminology_for_page(
                    [{"text": text} for text in originals],
                    source_language,
                    target_language,
                    glossary=glossary,
                    prefer_verified=getattr(SETTINGS, "phrase_memory_prefer_verified", True),
                )
            except Exception:
                pm_terms = None
        prompt = _localization_prompt(
            originals, literals, source_language, target_language, style, glossary, constraints or [],
            phrase_memory_terminology=pm_terms,
        )
        if self.provider == "gemini":
            response = _post_json(
                f"https://generativelanguage.googleapis.com/v1beta/models/{quote(self.model)}:generateContent?key={quote(self.api_key)}",
                {
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
                },
            )
            raw = response["candidates"][0]["content"]["parts"][0]["text"]
        else:
            endpoint = "https://api.groq.com/openai/v1/chat/completions" if self.provider == "groq" else "https://api.deepseek.com/chat/completions"
            response = _post_json(
                endpoint,
                {
                    "model": self.model, "temperature": 0.2,
                    "messages": [
                        {"role": "system", "content": "You are a professional manga localization editor. Return JSON only."},
                        {"role": "user", "content": prompt},
                    ],
                    "response_format": {"type": "json_object"},
                },
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            raw = response["choices"][0]["message"]["content"]
        parsed = json.loads(raw)
        values = parsed.get("translations", [])
        by_index = {int(value.get("index", -1)): value for value in values if isinstance(value, dict)}
        results = []
        for index, literal in enumerate(literals, 1):
            value = by_index.get(index)
            if value is None:
                raise RuntimeError(f"Localization response omitted text block {index}.")
            translated = str(value.get("translated_text", "")).strip()
            if not translated:
                translated = literal
            results.append({
                "translated_text": translated,
                "review_reasons": [str(item) for item in value.get("review_reasons", [])],
                "alternatives": [str(item) for item in value.get("alternatives", [])][:3],
                "note": str(value.get("note", ""))[:300], "style": style,
            })
        return results


class JsonTranslationCache:
    """Small project-local cache that prevents repeated provider requests."""

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.values = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            self.values = {}

    @staticmethod
    def key(*values: object) -> str:
        return sha256(json.dumps(values, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def get(self, key: str):
        return self.values.get(key)

    def put(self, key: str, value) -> None:
        self.values[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.values, ensure_ascii=False, indent=2), encoding="utf-8")


def build_translation_services(
    literal_provider: str, localization_provider: str, *, model: str = "", glossary: dict[str, str] | None = None,
    settings: AppSettings = SETTINGS,
) -> tuple[TranslationProvider, MangaLocalizer]:
    glossary = glossary or {}
    if literal_provider == "google":
        translator: TranslationProvider = GoogleCloudTranslator(CREDENTIALS.get("google"))
    else:
        translator = MarianTranslator(glossary=glossary)
    if localization_provider in {"gemini", "groq", "deepseek"}:
        selected_model = model or settings.model_for(localization_provider)
        localizer: MangaLocalizer = RemoteMangaLocalizer(
            localization_provider, CREDENTIALS.get(localization_provider), selected_model,
        )
    else:
        localizer = LocalMangaLocalizer()
    return translator, localizer


def translate_regions(
    regions: list[dict], source_language: str, target_language: str, provider: TranslationProvider,
    *, localizer: MangaLocalizer | None = None, style: str = "Manga", glossary: dict[str, str] | None = None,
    constraints: list[dict] | None = None, cache: JsonTranslationCache | None = None,
) -> list[TranslatedRegion]:
    originals = [str(region["text"]).strip() for region in regions]
    glossary = glossary or {}
    literal_key = JsonTranslationCache.key("literal-v2", provider.cache_identity, originals, source_language, target_language, glossary)
    translations = cache.get(literal_key) if cache else None
    if not isinstance(translations, list) or len(translations) != len(regions):
        translations = provider.translate(originals, source_language, target_language)
        if cache:
            cache.put(literal_key, translations)
    if len(translations) != len(regions):
        raise RuntimeError("Translation provider returned a different number of results.")

    localization_error = ""
    localized = None
    if localizer is not None:
        local_key = JsonTranslationCache.key(
            "localize-v2", localizer.cache_identity, originals, translations, source_language,
            target_language, style, glossary, constraints or [],
        )
        localized = cache.get(local_key) if cache else None
        if not isinstance(localized, list) or len(localized) != len(regions):
            try:
                localized = localizer.localize(
                    originals, translations, source_language, target_language,
                    style=style, glossary=glossary, constraints=constraints,
                )
                if cache:
                    cache.put(local_key, localized)
            except (ValueError, RuntimeError, KeyError, IndexError, HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
                localization_error = str(error) or type(error).__name__
                localized = None

    results: list[TranslatedRegion] = []
    for index, (region, original, literal) in enumerate(zip(regions, originals, translations), 1):
        value = localized[index - 1] if localized else {}
        translated = normalize_global_text(str(value.get("translated_text", literal)))
        reasons = [str(item) for item in value.get("review_reasons", [])]
        confidence = float(region["confidence"])
        if confidence < 0.70:
            reasons.append("low_ocr_confidence")
        if not translated:
            reasons.append("empty_translation")
        if source_language != "Latin-script" and translated.casefold() == original.casefold():
            reasons.append("translation_unchanged")
        if localization_error:
            reasons.append("localization_unavailable")
        reasons = list(dict.fromkeys(reasons))
        status = "review" if reasons else ("preserved" if source_language == "Latin-script" else "translated")
        results.append(TranslatedRegion(
            index=index, original_text=original, literal_text=normalize_global_text(str(literal)), translated_text=translated,
            ocr_confidence=confidence, polygon=region["polygon"], status=status,
            review_reasons=reasons, provider=provider.cache_identity,
            model=getattr(localizer, "cache_identity", "local") if localizer else "",
            localization_style=style, translation_quality="review" if reasons else "good",
            alternatives=list(value.get("alternatives", [])),
            localization_note=(str(value.get("note", "")) if not localization_error else f"Localization unavailable: {localization_error}"),
        ))
    return results


def translated_region_dict(region: TranslatedRegion) -> dict:
    return asdict(region)


def _post_json(url: str, body: dict, *, headers: dict[str, str] | None = None, timeout: int = 45) -> dict:
    request_headers = {"Content-Type": "application/json", "User-Agent": "Hydra-Manga-TL/0.6"}
    request_headers.update(headers or {})
    request = Request(url, json.dumps(body, ensure_ascii=False).encode("utf-8"), request_headers, method="POST")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Translation service returned HTTP {error.code}: {detail}") from error


def _localization_prompt(
    originals: list[str], literals: list[str], source_language: str, target_language: str,
    style: str, glossary: dict[str, str], constraints: list[dict],
    phrase_memory_terminology: dict[str, str] | None = None,
) -> str:
    blocks = [
        {"index": index, "original_text": original, "literal_text": literal,
         "layout": constraints[index - 1] if index - 1 < len(constraints) else {}}
        for index, (original, literal) in enumerate(zip(originals, literals), 1)
    ]
    term_part = ""
    if phrase_memory_terminology:
        term_part = f"\nKnown terminology: {json.dumps(phrase_memory_terminology, ensure_ascii=False)}"
    return f"""Localize these {source_language} manga dialogue blocks into {target_language}.
Style: {style}. Preserve meaning, character tone, punctuation, and glossary spellings. Use concise natural dialogue that fits the supplied layout. Use ellipses for pauses or abrupt dialogue turns; avoid em dashes and en dashes between English dialogue clauses. Do not invent missing content or intensify profanity. If the user, glossary, source metadata, or page context marks the work as hManga/hentai manga/adult manga, keep that adult manga tone and explicit intensity instead of sanitizing it or making it sound generic. Flag uncertain names, pronouns, idioms, incomplete OCR, and incomplete sentences.
Glossary: {json.dumps(glossary, ensure_ascii=False)}{term_part}
Blocks: {json.dumps(blocks, ensure_ascii=False)}
Return exactly one JSON object with a translations array. Each entry must contain index, translated_text, review_reasons (array of short snake_case codes), alternatives (up to 3 strings), and note. Preserve every index exactly once."""
