"""Application preferences and API credential storage."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json
import os
from pathlib import Path

from hydra_manga_tl.core.paths import PATHS, AppPaths


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


def _default_app_data_root() -> str:
    return str(AppPaths.default_root().resolve())


@dataclass
class AppSettings:
    app_data_root: str = field(default_factory=_default_app_data_root)
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
    fast_worker_override: int = 0
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
    title_reconstruction_shortcut: str = "Ctrl+F"
    manual_region_mode: str = "rectangle"
    filmstrip_collapse_mode: str = "current"
    translate_titles: bool = True
    translate_sfx: bool = True
    translate_signs: bool = True
    translate_credits: bool = True
    translation_memory_enabled: bool = True
    translation_memory_auto_learn: bool = True
    translation_memory_store_user_edits: bool = True
    translation_memory_prefer_verified: bool = True
    phrase_memory_enabled: bool = True
    phrase_memory_auto_learn: bool = True
    phrase_memory_store_user_edits: bool = True
    phrase_memory_prefer_verified: bool = True
    title_reconstruction_provider: str = "opencv"
    reconstruction_analysis_provider: str = "none"
    ai_enabled: bool = True
    ai_data_root: str = r"D:\HydraMangaAiData"
    ai_auto_train: bool = True

    @classmethod
    def load(cls, paths: AppPaths = PATHS) -> "AppSettings":
        try:
            payload = json.loads(paths.settings.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            settings = cls(app_data_root=str(paths.root))
            paths.configure(settings.app_data_root)
            return settings
        known = cls.__dataclass_fields__
        values = {key: value for key, value in payload.items() if key in known}
        app_data_root = str(values.get("app_data_root") or "").strip()
        if app_data_root:
            try:
                paths.configure(app_data_root)
                values["app_data_root"] = str(paths.root)
            except (OSError, RuntimeError, ValueError):
                values["app_data_root"] = str(paths.root)
        else:
            values["app_data_root"] = str(paths.root)
        groq_replacements = DEPRECATED_MODEL_REPLACEMENTS["groq"]
        if values.get("groq_model") in groq_replacements:
            values["groq_model"] = groq_replacements[values["groq_model"]]
        if values.get("filmstrip_collapse_mode") not in {None, "current", "always_collapsed"}:
            values["filmstrip_collapse_mode"] = "current"
        return cls(**values)

    def save(self, paths: AppPaths = PATHS) -> None:
        paths.root.mkdir(parents=True, exist_ok=True)
        values = asdict(self)
        if paths is not PATHS:
            try:
                configured_root = Path(str(self.app_data_root)).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                configured_root = paths.root
            if not self.app_data_root or configured_root == AppPaths.default_root().resolve():
                values["app_data_root"] = str(paths.root)
        payload = json.dumps(values, indent=2)
        paths.settings.write_text(payload, encoding="utf-8")
        default_root = AppPaths.default_root().resolve()
        if paths is PATHS and paths.root != default_root:
            default_root.mkdir(parents=True, exist_ok=True)
            (default_root / "settings.json").write_text(payload, encoding="utf-8")

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
