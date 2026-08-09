"""Cancellable background pipeline using the validated OCR/translation/render engines."""

from __future__ import annotations

import json
import gc
import hashlib
import logging
import re
import shutil
import threading
import tempfile
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw

from PySide6.QtCore import QObject, Signal, Slot

from hydra_manga_tl.phase.context_engine import ContextEngine
from hydra_manga_tl.phase.intelligent_page import IntelligentPageResult
from hydra_manga_tl.phase.job_manifest import JobManifest
from hydra_manga_tl.phase.pipeline_helpers import (
    _auto_translate_region_type,
    _box_from_polygon,
    _bubble_display_id,
    _cache_fragment,
    _classify_bubble,
    _completed_project_output,
    _existing_artifacts,
    _file_digest,
    _initial_ocr_language,
    _model_identity,
    _model_identity_payload,
    _needs_auto_ocr_fallback,
    _normalized_dialogue_payload,
    _ocr_cache_key,
    _ocr_engine_languages,
    _ocr_settings_fingerprint,
    _page_translation_cache_key,
    _preprocessing_settings_fingerprint,
    _preprocessing_stage_fingerprint,
    _project_resume_config,
    _provider_identity,
    _record_stage_completion,
    _render_settings_fingerprint,
    _render_stage_fingerprint,
    _resume_policy_payload,
    _source_text_color,
    _stable_bubble_id,
    _stable_json_hash,
    _state_manager_for_stages,
    _text_group_without_preserved_marks,
    _translation_settings_fingerprint,
    _translation_stage_fingerprint,
    _write_json_atomic,
)
from hydra_manga_tl.phase.pipeline_worker_artifacts import (
    PipelineWorkerArtifactsMixin,
)
from hydra_manga_tl.phase.pipeline_worker_fast_prep import (
    PipelineWorkerFastPrepMixin,
)
from hydra_manga_tl.phase.pipeline_worker_fast_commit import (
    PipelineWorkerFastCommitMixin,
)
from hydra_manga_tl.phase.pipeline_worker_fast_run import PipelineWorkerFastRunMixin
from hydra_manga_tl.phase.pipeline_worker_fast_translate import (
    PipelineWorkerFastTranslateMixin,
)
from hydra_manga_tl.phase.pipeline_worker_render import PipelineWorkerRenderMixin
from hydra_manga_tl.phase.pipeline_worker_resume import PipelineWorkerResumeMixin
from hydra_manga_tl.phase.pipeline_worker_sequential import PipelineWorkerSequentialMixin
from hydra_manga_tl.phase.pipeline_worker_sequential_grouping import (
    PipelineWorkerSequentialGroupingMixin,
)
from hydra_manga_tl.phase.pipeline_worker_sequential_ocr import (
    PipelineWorkerSequentialOcrMixin,
)
from hydra_manga_tl.phase.pipeline_worker_sequential_render import (
    PipelineWorkerSequentialRenderMixin,
)
from hydra_manga_tl.phase.pipeline_worker_sequential_translation import (
    PipelineWorkerSequentialTranslationMixin,
)
from hydra_manga_tl.phase.pipeline_worker_translation import PipelineWorkerTranslationMixin
from hydra_manga_tl.phase.pipeline_service_events import PipelineServiceEventsMixin
from hydra_manga_tl.phase.pipeline_service_execution import PipelineServiceExecutionMixin
from hydra_manga_tl.phase.pipeline_service_planning import PipelineServicePlanningMixin
from hydra_manga_tl.core.language import resolve_source_language
from hydra_manga_tl.phase.layout import (
    TextGroup,
    classify_text_group,
    decorative_symbols_from_regions,
    group_regions,
    is_decorative_mark_text,
)
from hydra_manga_tl.phase.layout_graph import build_layout_graph
from hydra_manga_tl.ocr.core import (
    FULL_PAGE_OCR_MAX_SIDE,
    OCRResult,
    clean_ocr_text,
)
from hydra_manga_tl.ocr.service import OCRService, current_rss_mb
from hydra_manga_tl.core.paths import PATHS
from hydra_manga_tl.phase.preprocessor import prepare_ocr_image
from hydra_manga_tl.phase.review import review_translation_groups
from hydra_manga_tl.phase.segmentation import segment_bubble
from hydra_manga_tl.phase.stage_streaming import BoundedStageExecutor
from hydra_manga_tl.phase.state_manager import (
    PipelineDependencyGraph,
    PipelineStateManager,
    StageAction,
    StageContract,
    StageValidationRequest,
)
from hydra_manga_tl.title import detect_title_objects
from hydra_manga_tl.phase.typesetting import review_rendered_group, summarize_render_review
from hydra_manga_tl.translation.engines import PageTranslation
from hydra_manga_tl.translation.requests import RenderRequest
from hydra_manga_tl.translation.requests import TranslationRequestType
from hydra_manga_tl.translation.cache_store import TRANSLATION_CACHE
from hydra_manga_tl.translation.queue import RequestCancelled, TRANSLATION_QUEUE, TranslationQueue
from hydra_manga_tl.translation.runtime import TRANSLATION_RUNTIME
from hydra_manga_tl.translation.memory import (
    learn_validated_page,
    source_region_hash,
    source_text_hash,
)
from hydra_manga_tl.translation.scheduler import (
    ParallelPageJob,
    ParallelPageScheduler,
    PageTranslationOutcome,
    ProviderDispatcher,
    SchedulerSnapshot,
    SmartPageJob,
    SmartTranslationScheduler,
    resolve_worker_count,
    timed_stage,
)
from hydra_manga_tl.core.normalization import normalize_global_text
from hydra_manga_tl.core.region_types import normalize_region_type
from hydra_manga_tl.project.artifacts import (
    rendered_filename,
    target_manifest_path,
    target_render_dir,
    target_root,
    target_translation_path,
)
from hydra_manga_tl import __version__


LOGGER = logging.getLogger(__name__)


class PipelineWorker(
    PipelineWorkerArtifactsMixin,
    PipelineWorkerResumeMixin,
    PipelineWorkerRenderMixin,
    PipelineWorkerFastPrepMixin,
    PipelineWorkerFastCommitMixin,
    PipelineWorkerFastRunMixin,
    PipelineWorkerFastTranslateMixin,
    PipelineWorkerTranslationMixin,
    PipelineWorkerSequentialOcrMixin,
    PipelineWorkerSequentialGroupingMixin,
    PipelineWorkerSequentialTranslationMixin,
    PipelineWorkerSequentialRenderMixin,
    PipelineWorkerSequentialMixin,
    QObject,
):
    stage = Signal(str, str, int, int, str)
    image_finished = Signal(str, object)
    image_failed = Signal(str, str)
    finished = Signal(bool)
    scheduler_snapshot = Signal(object)

    def __init__(
        self,
        items: list[dict],
        artifacts: Path,
        target: str,
        cancel: threading.Event,
        config: dict | None = None,
        paths=None,
        translation_runtime=None,
        ocr_service_factory=None,
        scheduler_factory=None,
        logger=None,
    ) -> None:
        super().__init__()
        self.items = items
        self.artifacts = artifacts
        self.target = target
        self.cancel = cancel
        self.config = config or {}
        self._paths = paths
        self._translation_runtime = translation_runtime
        self._ocr_service_factory = ocr_service_factory
        self._scheduler_factory = scheduler_factory
        self._logger = logger
        self.target_artifacts = target_root(self.artifacts, self.target)

    def paths(self):
        if self._paths is not None:
            return self._paths
        from importlib import import_module

        return import_module("hydra_manga_tl.phase.pipeline").PATHS

    def translation_runtime(self):
        if self._translation_runtime is not None:
            return self._translation_runtime
        from importlib import import_module

        return import_module("hydra_manga_tl.phase.pipeline").TRANSLATION_RUNTIME

    def ocr_service_factory(self):
        if self._ocr_service_factory is not None:
            return self._ocr_service_factory
        from importlib import import_module

        return import_module("hydra_manga_tl.phase.pipeline").OCRService

    def scheduler_factory(self):
        if self._scheduler_factory is not None:
            return self._scheduler_factory
        from importlib import import_module

        return import_module("hydra_manga_tl.phase.pipeline").SmartTranslationScheduler

    def logger(self):
        if self._logger is not None:
            return self._logger
        from importlib import import_module

        return import_module("hydra_manga_tl.phase.pipeline").LOGGER

    def _run_ocr_page(
        self,
        ocr_service: OCRService,
        source_for_ocr: Path,
        *,
        preferred_language: str | None,
        quality: str,
        auto_language_fallback: bool,
        cache_path: Path,
        checkpoint_path: Path,
    ):
        return ocr_service.analyze_page(
            source_for_ocr,
            preferred_language=preferred_language,
            quality=quality,
            auto_language_fallback=auto_language_fallback,
            cache_path=cache_path,
            checkpoint_path=checkpoint_path,
        )

class PipelineService(
    PipelineServiceEventsMixin,
    PipelineServiceExecutionMixin,
    PipelineServicePlanningMixin,
    QObject,
):
    progress = Signal(str, str, int, int, str)
    image_finished = Signal(str, object)
    image_failed = Signal(str, str)
    completed = Signal(bool)
    request_state_changed = Signal(str, str, str)
    scheduler_stats = Signal(object)

    def __init__(self, queue: TranslationQueue | None = None, settings=None, paths=None) -> None:
        super().__init__()
        self._queue = queue or TRANSLATION_QUEUE
        self._settings = settings
        self._paths = paths
        self._future = None
        self._worker: PipelineWorker | None = None
        self._request_prefix_by_image: dict[str, str] = {}
        self._active_request_images: set[str] = set()
        self._next_request_type: TranslationRequestType | None = None
        self._next_force_retranslate = False
        self._queue.failed.connect(self._on_queue_request_failed)
        if hasattr(self._queue, "state_changed"):
            self._queue.state_changed.connect(self._on_queue_state_changed)

    def paths(self):
        if self._paths is not None:
            return self._paths
        from importlib import import_module

        return import_module("hydra_manga_tl.phase.pipeline").PATHS

    def set_request_type(self, request_type: TranslationRequestType | str) -> None:
        self._next_request_type = TranslationRequestType(request_type)

    def set_force_retranslate(self, force: bool) -> None:
        self._next_force_retranslate = bool(force)

    @property
    def running(self) -> bool:
        return self._future is not None
