"""Original-text playback using the operating system's installed voices."""

from __future__ import annotations

import logging
import sys
from typing import Callable

from PySide6.QtCore import QObject, QLocale, Signal
from PySide6.QtTextToSpeech import QTextToSpeech

from .ocr import clean_ocr_text
from .settings import SETTINGS


LOGGER = logging.getLogger(__name__)


LOCALE_BY_LANGUAGE = {
    "Japanese": "ja_JP",
    "Chinese": "zh_CN",
    "Latin-script": "en_US",
}

# Narrow corrections for confirmed OCR omissions whose dictionary reading is
# otherwise ambiguous. These affect speech only and never rewrite project text.
JAPANESE_SPEECH_OVERRIDES = {
    "何問題ない": "ナニモモンダイナイ",
    "何も問題ない": "ナニモモンダイナイ",
}


def _contains_japanese(text: str) -> bool:
    return any(
        "\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff"
        for char in text
    )


class JapaneseReadingConverter:
    """Lazily convert Japanese text to dictionary-backed kana readings."""

    def __init__(self, tokenizer_factory: Callable[[], object] | None = None) -> None:
        self._tokenizer_factory = tokenizer_factory or self._create_tokenizer
        self._tokenizer = None
        self._unavailable = False

    @staticmethod
    def _create_tokenizer():
        from sudachipy import dictionary

        return dictionary.Dictionary(dict="small").create()

    def convert(self, text: str) -> str:
        if not text or not _contains_japanese(text) or self._unavailable:
            return text
        for source, reading in JAPANESE_SPEECH_OVERRIDES.items():
            text = text.replace(source, reading)
        try:
            if self._tokenizer is None:
                self._tokenizer = self._tokenizer_factory()
            reading = []
            for morpheme in self._tokenizer.tokenize(text):
                surface = morpheme.surface()
                value = morpheme.reading_form()
                reading.append(value if value and _contains_japanese(surface) else surface)
            return "".join(reading) or text
        except Exception as error:
            self._unavailable = True
            LOGGER.warning("Japanese pronunciation conversion is unavailable: %s", error)
            return text


def prepare_speech_text(text: str, language: str, japanese_reader: JapaneseReadingConverter) -> str:
    cleaned = clean_ocr_text(text)
    return japanese_reader.convert(cleaned) if language == "Japanese" else cleaned


class SpeechService(QObject):
    unavailable = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        engines = QTextToSpeech.availableEngines()
        self.engine = QTextToSpeech("winrt", self) if sys.platform == "win32" and "winrt" in engines else QTextToSpeech(self)
        self.japanese_reader = JapaneseReadingConverter()

    def stop(self) -> None:
        self.engine.stop()

    def speak(self, text: str, language: str) -> bool:
        text = text.strip()
        if not text:
            return False
        if self.engine.state() == QTextToSpeech.State.Speaking:
            self.engine.stop()
            return True
        locale_name = LOCALE_BY_LANGUAGE.get(language, "")
        if not locale_name:
            self.unavailable.emit("The language for this text is unknown.")
            return False
        locale = QLocale(locale_name)
        candidates = [voice for voice in self.engine.availableVoices() if voice.locale().name().split("_")[0] == locale.name().split("_")[0]]
        if not candidates:
            language_name = QLocale.languageToString(locale.language())
            self.unavailable.emit(
                f"Hydra is using {language_name} for this text, but no matching speech voice is available. "
                "Install one from Windows Settings > Time & language > Speech. "
                "If the language is wrong, change Source at the top of the project."
            )
            return False
        preferred = {
            "Japanese": SETTINGS.japanese_voice,
            "Chinese": SETTINGS.chinese_voice,
            "Latin-script": SETTINGS.english_voice,
        }.get(language, "")
        voice = next((item for item in candidates if item.name() == preferred), candidates[0])
        self.engine.setLocale(locale)
        self.engine.setVoice(voice)
        self.engine.setRate(max(-1.0, min(1.0, float(SETTINGS.speech_rate))))
        self.engine.say(prepare_speech_text(text, language, self.japanese_reader))
        return True
