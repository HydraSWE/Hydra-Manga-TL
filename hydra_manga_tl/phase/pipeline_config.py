"""Pipeline configuration builders with injectable app settings."""

from __future__ import annotations

from typing import Any

from hydra_manga_tl.core.settings import SETTINGS


def pipeline_settings_config(settings=None) -> dict[str, Any]:
    settings = settings or SETTINGS
    return {
        "translation_engine": settings.translation_engine,
        "translation_fallback_engine": settings.translation_fallback_engine,
        "debug_artifacts_enabled": settings.debug_artifacts_enabled,
        "ocr_subprocess_enabled": settings.ocr_subprocess_enabled,
        "ocr_worker_recycle_pages": settings.ocr_worker_recycle_pages,
        "ocr_worker_memory_limit_mb": settings.ocr_worker_memory_limit_mb,
        "streaming_enabled": settings.streaming_enabled,
        "translation_concurrency": settings.translation_concurrency,
        "fast_worker_override": settings.fast_worker_override,
        "translation_memory_enabled": settings.translation_memory_enabled,
        "translation_memory_auto_learn": settings.translation_memory_auto_learn,
        "translation_memory_prefer_verified": settings.translation_memory_prefer_verified,
        "translate_title": settings.translate_titles,
        "translate_sfx": settings.translate_sfx,
        "translate_sign": settings.translate_signs,
        "translate_credit": settings.translate_credits,
        "qwen_model_path": settings.qwen_model_path,
        "qwen_model_name": settings.qwen_model_name,
        "provider_models": {
            "groq": settings.groq_model,
            "gemini": settings.gemini_model,
            "deepseek": settings.deepseek_model,
            "openai": settings.openai_model,
            "openai_compatible": settings.openai_compatible_model,
        },
        "provider_base_urls": {
            "openai_compatible": settings.openai_compatible_base_url,
        },
    }


def project_pipeline_config(project, settings=None) -> dict[str, Any]:
    config = {
        "project_id": project.id,
        "source_language": project.source_language,
        "quality": project.quality,
        "literal_provider": project.literal_provider,
        "localization_provider": project.localization_provider,
        "localization_model": project.localization_model,
        "localization_style": project.localization_style,
        "text_style": project.text_style,
        "auto_fit": project.auto_fit,
        "bubble_padding": project.bubble_padding,
        "max_lines": project.max_lines,
        "glossary": project.glossary,
    }
    config.update(pipeline_settings_config(settings))
    return config
