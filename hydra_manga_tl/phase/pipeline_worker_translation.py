"""Translation facade helpers for PipelineWorker."""

from __future__ import annotations

from typing import Any

from hydra_manga_tl.translation.engines import PageDialogue, PageTranslation


class PipelineWorkerTranslationMixin:
    @staticmethod
    def _build_page_dialogue(
        source_language: str,
        target_language: str,
        dialogue: list[dict[str, Any]],
        page_context: str,
    ) -> PageDialogue:
        return PageDialogue(
            source_language=source_language,
            target_language=target_language,
            dialogue=dialogue,
            page_context=page_context,
        )

    def _translate_page_dialogue(self, page: PageDialogue) -> PageTranslation:
        if self.cancel.is_set():
            raise RuntimeError("Translation request was cancelled")
        provider = str(self.config.get("translation_engine", "qwen") or "qwen").strip().lower()
        runtime = self.translation_runtime()
        logger = self.logger()
        if provider == "openai_compatible":
            provider_models = dict(self.config.get("provider_models", {}) or {})
            provider_base_urls = dict(self.config.get("provider_base_urls", {}) or {})
            logger.info(
                "OpenAI-compatible pipeline translation started units=%d base_url=%s model=%s",
                len(page.dialogue),
                provider_base_urls.get("openai_compatible", ""),
                provider_models.get("openai_compatible", ""),
            )
            try:
                result = runtime.translate_page(page, self.config)
            except Exception as error:
                logger.warning(
                    "OpenAI-compatible pipeline translation failed units=%d error=%s",
                    len(page.dialogue),
                    error,
                )
                raise
            logger.info(
                "OpenAI-compatible pipeline translation finished units=%d returned=%d provider=%s",
                len(page.dialogue),
                len(result.translations),
                runtime.last_engine_id,
            )
            return result
        return runtime.translate_page(page, self.config)
