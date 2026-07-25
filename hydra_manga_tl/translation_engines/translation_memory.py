from __future__ import annotations

import json
import threading
from hashlib import sha256
from pathlib import Path
from typing import Any

from ..paths import PATHS


class TranslationMemory:
    """Persistent exact-match translation memory shared across projects."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (PATHS.root / "translation_memory.json")
        self._lock = threading.RLock()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            payload = {}
        self.values: dict[str, str] = {
            str(key): str(value)
            for key, value in payload.items()
            if isinstance(value, str)
        }

    @staticmethod
    def key(
        *,
        engine_id: str,
        source_language: str,
        target_language: str,
        source_text: str,
        glossary: dict[str, str] | None = None,
    ) -> str:
        payload: dict[str, Any] = {
            "kind": "global-translation-memory-v1",
            "engine": engine_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_text": source_text.strip(),
            "glossary": glossary or {},
        }
        return sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()

    def get(
        self,
        *,
        engine_id: str,
        source_language: str,
        target_language: str,
        source_text: str,
        glossary: dict[str, str] | None = None,
    ) -> str | None:
        key = self.key(
            engine_id=engine_id,
            source_language=source_language,
            target_language=target_language,
            source_text=source_text,
            glossary=glossary,
        )
        with self._lock:
            return self.values.get(key)

    def put(
        self,
        *,
        engine_id: str,
        source_language: str,
        target_language: str,
        source_text: str,
        translated_text: str,
        glossary: dict[str, str] | None = None,
    ) -> None:
        source_text = source_text.strip()
        translated_text = translated_text.strip()
        if not source_text or not translated_text:
            return
        key = self.key(
            engine_id=engine_id,
            source_language=source_language,
            target_language=target_language,
            source_text=source_text,
            glossary=glossary,
        )
        with self._lock:
            if self.values.get(key) == translated_text:
                return
            self.values[key] = translated_text
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.values, ensure_ascii=False, indent=2), encoding="utf-8")
