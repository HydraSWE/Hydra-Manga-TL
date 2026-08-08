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
    "openai": "OPENAI_API_KEY",
    "openai_compatible": ("OPENAI_COMPATIBLE_API_KEY", "TOKENROUTER_API_KEY"),
}

DEPRECATED_MODEL_REPLACEMENTS = {
    "groq": {
        "qwen/qwen3-32b": "openai/gpt-oss-120b",
        "qwen/qwen3.6-27b": "openai/gpt-oss-120b",
    },
}


def _default_app_data_root() -> str:
    return str(AppPaths.default_root().resolve())


def _default_export_root() -> str:
    return str((Path.home() / "Hydra Manga TL Exports").resolve())


def _default_manga_import_root() -> str:
    return str(Path.home().resolve())


@dataclass
class AppSettings:
    app_data_root: str = field(default_factory=_default_app_data_root)
    export_root: str = field(default_factory=_default_export_root)
    project_import_root: str = ""
    manga_import_root: str = field(default_factory=_default_manga_import_root)
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
    openai_model: str = "gpt-4.1-mini"
    openai_compatible_name: str = "Kimi / TokenRouter"
    openai_compatible_base_url: str = "https://api.tokenrouter.com/v1"
    openai_compatible_model: str = "moonshotai/kimi-k3-free"
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
    # Notifications (flat fields, v1 — QSystemTrayIcon.showMessage)
    notif_enabled: bool = False
    notif_translation_completed: bool = True
    notif_translation_failed: bool = True
    notif_export_completed: bool = True
    notif_export_failed: bool = True
    notif_review_queue: bool = True
    notif_build_finished: bool = False    # future / disabled in UI
    notif_updates_available: bool = True
    updates_check_automatically: bool = True
    updates_prompt_before_download: bool = True
    updates_last_checked_at: str = ""
    updates_dismissed_version: str = ""

    @classmethod
    def load(cls, paths: AppPaths = PATHS) -> "AppSettings":
        source_path = (
            paths.profile_settings
            if paths.profile_settings.is_file()
            else paths.settings
        )
        try:
            payload = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            settings = cls(app_data_root=str(paths.root))
            paths.configure(settings.app_data_root)
            return settings
        if (
            paths.profile == "development"
            and source_path == paths.settings
        ):
            payload = {
                "app_data_root": payload.get("app_data_root", str(paths.root))
            }
        known = cls.__dataclass_fields__
        values = {key: value for key, value in payload.items() if key in known}
        app_data_root = str(values.get("app_data_root") or "").strip()
        if app_data_root:
            try:
                paths.configure(app_data_root)
                values["app_data_root"] = str(paths.root)
                if (
                    source_path != paths.profile_settings
                    and paths.profile_settings.is_file()
                ):
                    profile_payload = json.loads(
                        paths.profile_settings.read_text(encoding="utf-8")
                    )
                    values.update({
                        key: value
                        for key, value in profile_payload.items()
                        if key in known
                    })
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
        paths.profile_root.mkdir(parents=True, exist_ok=True)
        values = asdict(self)
        if paths is not PATHS:
            try:
                configured_root = Path(str(self.app_data_root)).expanduser().resolve()
            except (OSError, RuntimeError, ValueError):
                configured_root = paths.root
            if not self.app_data_root or configured_root == AppPaths.default_root().resolve():
                values["app_data_root"] = str(paths.root)
        payload = json.dumps(values, indent=2)
        paths.profile_settings.write_text(payload, encoding="utf-8")
        default_root = AppPaths.default_root().resolve()
        if paths is PATHS and paths.root != default_root:
            default_root.mkdir(parents=True, exist_ok=True)
            bootstrap = json.dumps(
                {"app_data_root": str(paths.root)},
                indent=2,
            )
            (default_root / "settings.json").write_text(
                bootstrap,
                encoding="utf-8",
            )

    def model_for(self, provider: str) -> str:
        return {
            "gemini": self.gemini_model,
            "groq": self.groq_model,
            "deepseek": self.deepseek_model,
            "openai": self.openai_model,
            "openai_compatible": self.openai_compatible_model,
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
        env_names = PROVIDER_ENV.get(provider, "")
        if isinstance(env_names, str):
            env_names = (env_names,)
        for env_name in env_names:
            env_value = os.environ.get(env_name, "").strip()
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
            try:
                keyring.delete_password(self.service, provider)
            except Exception:
                pass
            keyring.set_password(self.service, provider, value)
        else:
            try:
                keyring.delete_password(self.service, provider)
            except Exception:
                pass


SETTINGS = AppSettings.load()
CREDENTIALS = CredentialStore()
