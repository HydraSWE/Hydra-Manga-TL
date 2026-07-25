"""Application preferences and API credential storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import json
import os

from .paths import PATHS, AppPaths


PROVIDER_ENV = {
    "google": "GOOGLE_TRANSLATE_API_KEY",
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
}

DEPRECATED_MODEL_REPLACEMENTS = {
    "groq": {
        "qwen/qwen3-32b": "openai/gpt-oss-120b",
        "qwen/qwen3.6-27b": "openai/gpt-oss-120b",
    },
}


@dataclass
class AppSettings:
    literal_provider: str = "marian"
    localization_provider: str = "local"
    translation_engine: str = "qwen"
    translation_fallback_engine: str = "marian"
    debug_artifacts_enabled: bool = False
    ocr_subprocess_enabled: bool = True
    ocr_worker_recycle_pages: int = 25
    ocr_worker_memory_limit_mb: int = 2048
    streaming_enabled: bool = True
    translation_concurrency: int = 2
    qwen_model_path: str = ""
    qwen_model_name: str = "Qwen3-4B-Instruct-2507"
    qwen_model_status: str = "Not installed"
    gemini_model: str = "gemini-3.5-flash"
    groq_model: str = "openai/gpt-oss-120b"
    deepseek_model: str = "deepseek-v4-flash"
    speech_rate: float = 0.0
    japanese_voice: str = ""
    chinese_voice: str = ""
    english_voice: str = ""
    manual_textbox_shortcut: str = "Ctrl+D"
    ai_enabled: bool = True
    ai_data_root: str = r"D:\HydraMangaAiData"
    ai_auto_train: bool = True

    @classmethod
    def load(cls, paths: AppPaths = PATHS) -> "AppSettings":
        try:
            payload = json.loads(paths.settings.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return cls()
        known = cls.__dataclass_fields__
        values = {key: value for key, value in payload.items() if key in known}
        groq_replacements = DEPRECATED_MODEL_REPLACEMENTS["groq"]
        if values.get("groq_model") in groq_replacements:
            values["groq_model"] = groq_replacements[values["groq_model"]]
        return cls(**values)

    def save(self, paths: AppPaths = PATHS) -> None:
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.settings.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")

    def model_for(self, provider: str) -> str:
        return {
            "gemini": self.gemini_model,
            "groq": self.groq_model,
            "deepseek": self.deepseek_model,
        }.get(provider, "")


class CredentialStore:
    """Keep secrets out of JSON and project artifacts.

    The optional keyring package uses Windows Credential Manager in packaged
    builds. Environment variables remain useful for development and CI.
    """

    service = "Hydra Manga TL"

    @staticmethod
    def _keyring():
        try:
            import keyring
        except ImportError:
            return None
        return keyring

    def get(self, provider: str) -> str:
        env_value = os.environ.get(PROVIDER_ENV.get(provider, ""), "").strip()
        if env_value:
            return env_value
        keyring = self._keyring()
        if keyring is None:
            return ""
        try:
            return (keyring.get_password(self.service, provider) or "").strip()
        except Exception:
            return ""

    def set(self, provider: str, value: str) -> None:
        keyring = self._keyring()
        if keyring is None:
            raise RuntimeError("Secure credential storage is unavailable. Install the keyring package.")
        value = value.strip()
        if value:
            keyring.set_password(self.service, provider, value)
        else:
            try:
                keyring.delete_password(self.service, provider)
            except keyring.errors.PasswordDeleteError:
                pass


SETTINGS = AppSettings.load()
CREDENTIALS = CredentialStore()
