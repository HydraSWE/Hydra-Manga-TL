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


@dataclass
class AppSettings:
    literal_provider: str = "marian"
    localization_provider: str = "local"
    translation_engine: str = "qwen"
    qwen_model_path: str = ""
    qwen_model_name: str = "Qwen3-4B-Instruct-2507"
    qwen_model_status: str = "Not installed"
    gemini_model: str = "gemini-3.5-flash"
    groq_model: str = "qwen/qwen3-32b"
    deepseek_model: str = "deepseek-v4-flash"
    speech_rate: float = 0.0
    japanese_voice: str = ""
    chinese_voice: str = ""
    english_voice: str = ""

    @classmethod
    def load(cls, paths: AppPaths = PATHS) -> "AppSettings":
        try:
            payload = json.loads(paths.settings.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return cls()
        known = cls.__dataclass_fields__
        return cls(**{key: value for key, value in payload.items() if key in known})

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
