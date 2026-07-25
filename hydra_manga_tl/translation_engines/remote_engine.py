from __future__ import annotations

import html
import json
from urllib.parse import quote

from ..settings import CREDENTIALS, SETTINGS
from ..translation import SOURCE_CODES, _post_json
from .base import PageDialogue, PageTranslation
from .prompts import SYSTEM_PROMPT, build_page_prompt
from .qwen_engine import extract_first_json_object


class GooglePageEngine:
    engine_id = "google"

    def __init__(self, *, api_key: str | None = None, glossary: dict[str, str] | None = None, **_: object) -> None:
        self.api_key = api_key if api_key is not None else CREDENTIALS.get("google")
        self.glossary = glossary or {}

    def load(self) -> None:
        if not self.api_key:
            raise ValueError("Google Cloud Translation API key is not configured.")

    def translate_page(self, page: PageDialogue) -> PageTranslation:
        self.load()
        dialogue = page.dialogue
        texts = [str(item.get("text", "")).strip() for item in dialogue]
        if not texts:
            return PageTranslation(page.source_language, page.target_language, [])
        body = {
            "q": texts,
            "source": SOURCE_CODES.get(page.source_language, ""),
            "target": page.target_language,
            "format": "text",
        }
        payload = _post_json(
            f"https://translation.googleapis.com/language/translate/v2?key={quote(self.api_key)}",
            body,
        )
        values = payload.get("data", {}).get("translations", [])
        translated = [html.unescape(str(value.get("translatedText", ""))).strip() for value in values]
        if len(translated) != len(texts):
            raise RuntimeError("Google Translation returned an unexpected number of results.")
        return PageTranslation(
            page.source_language,
            page.target_language,
            [{"id": str(item.get("id")), "text": translated[index]} for index, item in enumerate(dialogue)],
        )

    def unload(self) -> None:
        return


class ChatCompletionPageEngine:
    provider = ""
    endpoint = ""
    default_model = ""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
        glossary: dict[str, str] | None = None,
        temperature: float = 0.1,
        **_: object,
    ) -> None:
        self.api_key = api_key if api_key is not None else CREDENTIALS.get(self.provider)
        self.model = model or SETTINGS.model_for(self.provider) or self.default_model
        self.glossary = glossary or {}
        self.temperature = temperature

    @property
    def engine_id(self) -> str:
        return f"{self.provider}:{self.model}"

    def load(self) -> None:
        if not self.api_key:
            raise ValueError(f"{self.provider.title()} API key is not configured.")
        if not self.model:
            raise ValueError(f"{self.provider.title()} model is not configured.")

    def translate_page(self, page: PageDialogue) -> PageTranslation:
        self.load()
        if not page.dialogue:
            return PageTranslation(page.source_language, page.target_language, [])
        dialogue = [{"id": str(item.get("id", "")), "text": str(item.get("text", "")).strip()} for item in page.dialogue]
        prompt = build_page_prompt(
            source_language=page.source_language,
            target_language=page.target_language,
            style="Manga",
            glossary=self.glossary,
            dialogue=dialogue,
            temperature=self.temperature,
            page_context=page.page_context,
        )
        payload = _post_json(
            self.endpoint,
            {
                "model": self.model,
                "temperature": self.temperature,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        raw = payload["choices"][0]["message"]["content"]
        return _page_translation_from_json(page, raw, self.provider)

    def unload(self) -> None:
        return


class GroqPageEngine(ChatCompletionPageEngine):
    provider = "groq"
    endpoint = "https://api.groq.com/openai/v1/chat/completions"
    default_model = "qwen/qwen3-32b"


class DeepSeekPageEngine(ChatCompletionPageEngine):
    provider = "deepseek"
    endpoint = "https://api.deepseek.com/chat/completions"
    default_model = "deepseek-chat"


class GeminiPageEngine(ChatCompletionPageEngine):
    provider = "gemini"
    default_model = "gemini-3.5-flash"

    @property
    def engine_id(self) -> str:
        return f"gemini:{self.model}"

    def translate_page(self, page: PageDialogue) -> PageTranslation:
        self.load()
        if not page.dialogue:
            return PageTranslation(page.source_language, page.target_language, [])
        dialogue = [{"id": str(item.get("id", "")), "text": str(item.get("text", "")).strip()} for item in page.dialogue]
        prompt = build_page_prompt(
            source_language=page.source_language,
            target_language=page.target_language,
            style="Manga",
            glossary=self.glossary,
            dialogue=dialogue,
            temperature=self.temperature,
            page_context=page.page_context,
        )
        payload = _post_json(
            f"https://generativelanguage.googleapis.com/v1beta/models/{quote(self.model)}:generateContent?key={quote(self.api_key)}",
            {
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": self.temperature, "responseMimeType": "application/json"},
            },
        )
        raw = payload["candidates"][0]["content"]["parts"][0]["text"]
        return _page_translation_from_json(page, raw, self.provider)


def _page_translation_from_json(page: PageDialogue, raw: str, provider: str) -> PageTranslation:
    payload = extract_first_json_object(raw)
    values = payload.get("translations", [])
    if not isinstance(values, list):
        raise RuntimeError(f"{provider.title()} response did not include a translations array")
    by_id: dict[str, str] = {}
    for item in values:
        if not isinstance(item, dict):
            continue
        entry_id = str(item.get("id", "")).strip()
        entry_text = str(item.get("text", "") or item.get("translated_text", "")).strip()
        if entry_id:
            by_id[entry_id] = entry_text
    translations = []
    for item in page.dialogue:
        entry_id = str(item.get("id", "")).strip()
        if entry_id not in by_id:
            raise RuntimeError(f"{provider.title()} response omitted id {entry_id}")
        translations.append({"id": entry_id, "text": by_id[entry_id]})
    return PageTranslation(page.source_language, page.target_language, translations)
