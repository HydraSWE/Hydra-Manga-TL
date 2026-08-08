from __future__ import annotations

import html
import json
import logging
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from hydra_manga_tl.core.settings import CREDENTIALS, SETTINGS
from hydra_manga_tl.translation.service import (
    SOURCE_CODES,
    TranslationHTTPError,
    _post_json,
    _retry_after_seconds,
)
from .base import PageDialogue, PageTranslation, prepare_dialogue_item
from .prompts import SYSTEM_PROMPT, build_page_prompt
from .qwen_engine import extract_first_json_object


LOGGER = logging.getLogger(__name__)


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
    request_timeout: int | None = None

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
        dialogue = [prepare_dialogue_item(item) for item in page.dialogue]
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
        prompt = build_page_prompt(
            source_language=page.source_language,
            target_language=page.target_language,
            style="Manga",
            glossary=self.glossary,
            dialogue=dialogue,
            temperature=self.temperature,
            page_context=page.page_context,
            phrase_memory_terminology=pm_terms,
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
            **({"timeout": self.request_timeout} if self.request_timeout else {}),
        )
        raw = payload["choices"][0]["message"]["content"]
        return _page_translation_from_json(page, raw, self.provider)

    def unload(self) -> None:
        return


class OpenAIPageEngine(ChatCompletionPageEngine):
    provider = "openai"
    endpoint = "https://api.openai.com/v1/chat/completions"
    default_model = "gpt-4.1-mini"


class OpenAICompatiblePageEngine(ChatCompletionPageEngine):
    provider = "openai_compatible"
    default_model = "moonshotai/kimi-k3-free"
    request_timeout = 120

    def __init__(
        self,
        *,
        base_url: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(**kwargs)
        configured = (base_url or SETTINGS.openai_compatible_base_url).strip()
        self.base_url = configured.rstrip("/") or "https://api.tokenrouter.com/v1"
        self.endpoint = f"{self.base_url}/chat/completions"

    @property
    def engine_id(self) -> str:
        return f"{self.provider}:{self.base_url}:{self.model}"

    def translate_page(self, page: PageDialogue) -> PageTranslation:
        self.load()
        if not page.dialogue:
            return PageTranslation(page.source_language, page.target_language, [])
        dialogue = [prepare_dialogue_item(item) for item in page.dialogue]
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
        prompt = build_page_prompt(
            source_language=page.source_language,
            target_language=page.target_language,
            style="Manga",
            glossary=self.glossary,
            dialogue=dialogue,
            temperature=self.temperature,
            page_context=page.page_context,
            phrase_memory_terminology=pm_terms,
        )
        LOGGER.info(
            "OpenAI-compatible translation request started base_url=%s model=%s units=%d",
            self.base_url,
            self.model,
            len(dialogue),
        )
        raw = _post_chat_completion_stream(
            self.endpoint,
            {
                "model": self.model,
                "temperature": self.temperature,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=self.request_timeout,
        )
        LOGGER.info(
            "OpenAI-compatible translation response received base_url=%s model=%s units=%d chars=%d",
            self.base_url,
            self.model,
            len(dialogue),
            len(raw),
        )
        return _page_translation_from_json(page, raw, self.provider)


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
        dialogue = [prepare_dialogue_item(item) for item in page.dialogue]
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
        prompt = build_page_prompt(
            source_language=page.source_language,
            target_language=page.target_language,
            style="Manga",
            glossary=self.glossary,
            dialogue=dialogue,
            temperature=self.temperature,
            page_context=page.page_context,
            phrase_memory_terminology=pm_terms,
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
        
    expected_ids = [str(item.get("id", "")).strip() for item in page.dialogue]
    by_id: dict[str, str] = {}
    
    for item in values:
        if not isinstance(item, dict):
            continue
        entry_id = str(item.get("id", "")).strip()
        entry_text = str(item.get("text", "") or item.get("translated_text", "")).strip()
        if not entry_id:
            if len(expected_ids) == 1 and len(values) == 1:
                entry_id = expected_ids[0]
            else:
                continue
        by_id[entry_id] = entry_text
        
    if len(expected_ids) == 1 and len(by_id) == 1 and expected_ids[0] not in by_id:
        only_text = next(iter(by_id.values()))
        by_id = {expected_ids[0]: only_text}
        
    translations = []
    for entry_id in expected_ids:
        if entry_id not in by_id:
            raise RuntimeError(f"{provider.title()} response omitted id {entry_id}")
        translations.append({"id": entry_id, "text": by_id[entry_id]})
    return PageTranslation(page.source_language, page.target_language, translations)


def _post_chat_completion_stream(
    url: str,
    body: dict,
    *,
    headers: dict[str, str] | None = None,
    timeout: int = 120,
) -> str:
    request_headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
        "User-Agent": "Hydra-Manga-TL/0.6",
    }
    request_headers.update(headers or {})
    request = Request(
        url,
        json.dumps(body, ensure_ascii=False).encode("utf-8"),
        request_headers,
        method="POST",
    )
    parts: list[str] = []
    try:
        with urlopen(request, timeout=timeout) as response:
            for raw_line in response:
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":"):
                    continue
                if line.startswith("data:"):
                    line = line[5:].strip()
                if line == "[DONE]":
                    break
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for choice in chunk.get("choices", []) or []:
                    delta = choice.get("delta") or {}
                    content = delta.get("content")
                    if content:
                        parts.append(str(content))
                    message = choice.get("message") or {}
                    message_content = message.get("content")
                    if message_content:
                        parts.append(str(message_content))
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:500]
        retry_after = (
            error.headers.get("Retry-After")
            if error.headers is not None
            else None
        )
        LOGGER.warning(
            "OpenAI-compatible streaming request returned HTTP %s url=%s detail=%s",
            error.code,
            url,
            detail,
        )
        raise TranslationHTTPError(
            error.code,
            detail,
            retry_after=_retry_after_seconds(retry_after),
        ) from error
    except Exception as error:
        LOGGER.warning(
            "OpenAI-compatible streaming request failed url=%s error=%s",
            url,
            error,
        )
        raise
    content = "".join(parts).strip()
    if not content:
        raise RuntimeError("OpenAI-compatible stream ended without message content")
    return content
