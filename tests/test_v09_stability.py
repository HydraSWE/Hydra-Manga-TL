from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    site_packages = Path(__file__).resolve().parents[1] / ".venv" / "Lib" / "site-packages"
    for dll_dir in (site_packages / "PySide6", site_packages / "shiboken6"):
        if dll_dir.is_dir():
            os.add_dll_directory(str(dll_dir))

import numpy as np
from PIL import Image
from PySide6.QtCore import QObject, Signal

from hydra_manga_tl.project.export import export_archive, export_images
from hydra_manga_tl.phase.job_manifest import JobManifest
from hydra_manga_tl.project.manual_region import _generic_title_composition, _ordered_title_members
from hydra_manga_tl.ocr.core import OCRResult, PaddleOCREngine, TextRegion
from hydra_manga_tl.ocr.manager import SmartOCRManager
from hydra_manga_tl.ocr.runtime import OCRRuntimeManager, OCRWorkerClient, OCRWorkerCrashed
from hydra_manga_tl.ocr.service import OCRRetryStatsStore, OCRService
from hydra_manga_tl.ocr.worker import _write_warmup_image
from hydra_manga_tl.phase.pipeline import PipelineService
from hydra_manga_tl.phase.phase3 import _title_render_group, renderer_for_region
from hydra_manga_tl.phase.pipeline import _auto_translate_region_type
from hydra_manga_tl.project.model import ImageRecord, MangaProject
from hydra_manga_tl.core.region_types import group_region_type, is_title_like_region, normalize_region_type
from hydra_manga_tl.core.settings import AppSettings
from hydra_manga_tl.core.paths import AppPaths
from hydra_manga_tl.core.startup import StartupCoordinator
from hydra_manga_tl.title.models import TitleComposition
from hydra_manga_tl.title.style_profile import FillProfile, OutlineProfile, TitleStyleProfile
from hydra_manga_tl.translation.engines.base import PageDialogue, PageTranslation
from hydra_manga_tl.translation.engines.model_manager import TranslationEngineManager
from hydra_manga_tl.translation.engines.registry import EngineRegistration, TRANSLATION_PROVIDER_REGISTRY
from hydra_manga_tl.translation.engines.translation_memory import TranslationMemory
from hydra_manga_tl.translation.queue import CancellationToken, RequestCancelled
from hydra_manga_tl.translation.requests import TranslationRequest, TranslationRequestStatus, TranslationRequestType
from hydra_manga_tl.project.workspace import WorkspaceManager


def _region(text: str, confidence: float, x: int) -> TextRegion:
    return TextRegion(text, confidence, [[x, 20], [x + 20, 20], [x + 20, 50], [x, 50]])


def _result(regions: list[TextRegion]) -> OCRResult:
    return OCRResult(
        source="page.png",
        model_language="japan",
        language="Japanese",
        language_confidence=0.8,
        average_ocr_confidence=sum(item.confidence for item in regions) / len(regions) if regions else 0.0,
        regions=regions,
        language_scripts={},
    )


class _RetryEngine:
    def __init__(self, regions: list[TextRegion]) -> None:
        self.page = _result(regions)
        self.selection_calls: list[list[int]] = []

    def analyze(self, image_path, preferred_language=None):
        return self.page

    def analyze_selection(self, image_path, rect, **kwargs):
        self.selection_calls.append(list(rect))
        return _result([])


class _FakeTranslationEngine:
    engine_id = "fake"

    def __init__(self) -> None:
        self.loaded = False

    def load(self):
        self.loaded = True

    def unload(self):
        self.loaded = False

    def translate_page(self, page):
        return PageTranslation(page.source_language, page.target_language, [
            {"id": item["id"], "text": "translated"} for item in page.dialogue
        ])


class V09StabilityTests(unittest.TestCase):
    def make_image(self, folder: str) -> Path:
        path = Path(folder) / "page.png"
        Image.new("RGB", (480, 320), "white").save(path)
        return path

    def test_cloud_manager_constructs_only_selected_engine(self):
        constructed: list[str] = []

        def factory(name):
            def create(**kwargs):
                constructed.append(name)
                return _FakeTranslationEngine()
            return create

        replacements = {
            key: EngineRegistration(key, key, factory(key), cloud=key in {"groq", "google", "gemini"})
            for key in TRANSLATION_PROVIDER_REGISTRY
        }
        with patch.dict(TRANSLATION_PROVIDER_REGISTRY, replacements, clear=True):
            manager = TranslationEngineManager(preferred_engine="groq", fallback_engine="marian")
            manager.load()
        self.assertEqual(constructed, ["groq"])
        self.assertNotIn("qwen", manager.engines)
        self.assertNotIn("marian", manager.engines)

    def test_cloud_manager_does_not_construct_local_fallback_after_failure(self):
        constructed: list[str] = []

        class FailingCloudEngine(_FakeTranslationEngine):
            def translate_page(self, page):
                raise RuntimeError("cloud unavailable")

        def factory(name):
            def create(**kwargs):
                constructed.append(name)
                return FailingCloudEngine() if name == "groq" else _FakeTranslationEngine()
            return create

        replacements = {
            key: EngineRegistration(key, key, factory(key), cloud=key in {"groq", "google", "gemini", "deepseek"})
            for key in TRANSLATION_PROVIDER_REGISTRY
        }
        page = PageDialogue(
            source_language="Japanese",
            target_language="en",
            dialogue=[{"id": "r1", "text": "待て"}],
            page_context="",
        )
        with patch.dict(TRANSLATION_PROVIDER_REGISTRY, replacements, clear=True):
            manager = TranslationEngineManager(preferred_engine="groq", fallback_engine="marian")
            with self.assertRaises(RuntimeError):
                manager.translate_page(page)
        self.assertEqual(constructed, ["groq"])
        self.assertNotIn("marian", manager.engines)

    def test_manual_cloud_manager_can_use_configured_local_fallback(self):
        constructed: list[str] = []

        class FailingCloudEngine(_FakeTranslationEngine):
            def translate_page(self, page):
                raise RuntimeError("cloud unavailable")

        def factory(name):
            def create(**kwargs):
                constructed.append(name)
                return FailingCloudEngine() if name == "groq" else _FakeTranslationEngine()
            return create

        replacements = {
            key: EngineRegistration(key, key, factory(key), cloud=key in {"groq", "google", "gemini", "deepseek"})
            for key in TRANSLATION_PROVIDER_REGISTRY
        }
        page = PageDialogue(
            source_language="Japanese",
            target_language="en",
            dialogue=[{"id": "r1", "text": "待て"}],
            page_context="",
        )
        with patch.dict(TRANSLATION_PROVIDER_REGISTRY, replacements, clear=True):
            with tempfile.TemporaryDirectory() as folder:
                manager = TranslationEngineManager(
                    preferred_engine="groq",
                    fallback_engine="marian",
                    allow_local_fallback_for_cloud=True,
                    translation_memory=TranslationMemory(Path(folder) / "memory.json"),
                )
                result = manager.translate_page(page)
        self.assertEqual(constructed, ["groq", "marian"])
        self.assertTrue(result.translations[0]["text"])

    def test_settings_translation_test_honors_local_fallback_selection(self):
        from hydra_manga_tl.ui import TranslationTestWorker

        manager = Mock()
        manager.translate_page.return_value = PageTranslation(
            source_language="Japanese",
            target_language="en",
            translations=[{"id": "r1", "text": "Wait!"}],
        )
        completed: list[tuple[bool, str]] = []
        worker = TranslationTestWorker(
            qwen_model_path=None,
            preferred_engine="groq",
            fallback_engine="marian",
            qwen_model_name="qwen3-4b",
            provider_models={"groq": "test-model"},
        )
        worker.completed.connect(lambda ok, message: completed.append((ok, message)))
        with patch(
            "hydra_manga_tl.translation.engines.TranslationEngineManager",
            return_value=manager,
        ) as constructor:
            worker.run()

        self.assertEqual(completed, [(True, "Wait!")])
        self.assertTrue(constructor.call_args.kwargs["allow_local_fallback_for_cloud"])

    def test_primary_and_fallback_failures_are_reported_together(self):
        class FailingEngine(_FakeTranslationEngine):
            def __init__(self, message: str) -> None:
                super().__init__()
                self.message = message

            def translate_page(self, page):
                raise RuntimeError(self.message)

        replacements = {
            "groq": EngineRegistration("groq", "Groq", lambda **kwargs: FailingEngine("cloud timeout"), cloud=True),
            "marian": EngineRegistration("marian", "Marian", lambda **kwargs: FailingEngine("local model missing")),
        }
        page = PageDialogue(
            source_language="Japanese",
            target_language="en",
            dialogue=[{"id": "r1", "text": "待て"}],
            page_context="",
        )
        with patch.dict(TRANSLATION_PROVIDER_REGISTRY, replacements, clear=True):
            with tempfile.TemporaryDirectory() as folder:
                manager = TranslationEngineManager(
                    preferred_engine="groq",
                    fallback_engine="marian",
                    allow_local_fallback_for_cloud=True,
                    translation_memory=TranslationMemory(Path(folder) / "memory.json"),
                )
                with self.assertRaises(RuntimeError) as raised:
                    manager.translate_page(page)
        self.assertIn("cloud timeout", str(raised.exception))
        self.assertIn("fallback engine 'marian' also failed", str(raised.exception))
        self.assertIn("local model missing", str(raised.exception))

    def test_cloud_selection_does_not_start_marian_warmup(self):
        from hydra_manga_tl.translation import runtime as translation_runtime

        before = len(translation_runtime._WARMUP_THREADS)
        with patch.object(translation_runtime, "_warm_marian") as warm:
            translation_runtime.start_translation_warmup(translation_engine="groq")
        self.assertEqual(len(translation_runtime._WARMUP_THREADS), before)
        warm.assert_not_called()

    def test_entrypoint_freeze_support_runs_before_app_import(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertLess(
            source.index("multiprocessing.freeze_support()"),
            source.index("from hydra_manga_tl.core.application import MangaApplication"),
        )

    def test_frozen_editor_render_uses_isolated_phase3_process(self):
        with (
            patch("hydra_manga_tl.project.workspace.sys.frozen", True, create=True),
            patch("hydra_manga_tl.project.workspace.sys.executable", r"C:\Hydra\Hydra Manga TL.exe"),
        ):
            command = WorkspaceManager._editor_render_command(
                Path("page.json"),
                Path("rendered"),
                "complete",
            )

        self.assertEqual(command[0], r"C:\Hydra\Hydra Manga TL.exe")
        self.assertEqual(command[1], "--phase3-render")
        self.assertIn("page.json", command)
        self.assertIn("rendered", command)

    def test_editor_render_pumps_ui_events_while_child_is_running(self):
        process = Mock(returncode=0)
        process.communicate.side_effect = [
            subprocess.TimeoutExpired(["renderer"], 0.1),
            ("render complete\n", ""),
        ]
        with (
            patch("hydra_manga_tl.project.workspace.subprocess.Popen", return_value=process),
            patch.object(WorkspaceManager, "_process_editor_render_events") as pump_events,
        ):
            output = WorkspaceManager._run_editor_render(
                Path("page.json"),
                Path("rendered"),
            )

        pump_events.assert_called_once()
        self.assertEqual(output, ["render complete"])

    def test_editor_render_starts_phase3_from_repo_root(self):
        process = Mock(returncode=0)
        process.communicate.return_value = ("render complete\n", "")
        repo_root = Path(__file__).resolve().parents[1]

        with (
            patch("hydra_manga_tl.project.workspace.subprocess.Popen", return_value=process) as popen,
            patch.object(WorkspaceManager, "_process_editor_render_events"),
        ):
            output = WorkspaceManager._run_editor_render(
                Path("page.json"),
                Path("rendered"),
            )

        command = popen.call_args.args[0]
        kwargs = popen.call_args.kwargs
        self.assertEqual(command[1:3], ["-m", "hydra_manga_tl.phase.phase3"])
        self.assertEqual(kwargs["cwd"], str(repo_root))
        self.assertIn(str(repo_root), kwargs["env"]["PYTHONPATH"].split(os.pathsep))
        self.assertEqual(output, ["render complete"])

    def test_subprocess_mode_starts_global_ocr_runtime(self):
        from hydra_manga_tl.core.application import MangaApplication

        instance = MangaApplication.__new__(MangaApplication)
        instance.settings = AppSettings(
            ocr_subprocess_enabled=True,
            ocr_worker_recycle_pages=17,
            ocr_worker_memory_limit_mb=3072,
        )
        with patch("hydra_manga_tl.ocr.runtime.start_ocr_warmup") as ocr_warmup, \
                patch("hydra_manga_tl.ocr.runtime.start_ocr_runtime") as ocr_runtime, \
                patch("hydra_manga_tl.translation.runtime.start_translation_warmup"):
            instance.start_background_warmup()
        ocr_warmup.assert_not_called()
        ocr_runtime.assert_called_once_with(memory_limit_mb=3072, recycle_pages=17)

    def test_startup_progress_is_monotonic_and_ordered(self):
        coordinator = StartupCoordinator()
        emitted = []
        coordinator.progress_changed.connect(lambda stage, label, value: emitted.append((stage, label, value)))
        coordinator.advance("core", "Core", 20)
        coordinator.advance("late", "Late", 80)
        coordinator.advance("stale", "Stale", 40)
        self.assertEqual([20, 80, 80], [item[2] for item in emitted])
        self.assertEqual(80, coordinator.progress)

    def test_application_module_keeps_workspace_import_lazy(self):
        source = (Path(__file__).resolve().parents[1] / "hydra_manga_tl" / "core" / "application.py").read_text(encoding="utf-8")
        prefix = source[:source.index("class MangaApplication")]
        self.assertNotIn("\nfrom .ui import MainWindow", prefix)
        self.assertNotIn("\nfrom hydra_manga_tl.ocr.runtime import", prefix)

    def test_old_project_defaults_filmstrip_to_expanded(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "project.json"
            path.write_text('{"id":"old","name":"Old","version":6,"images":[]}', encoding="utf-8")
            project = MangaProject.load(path)
        self.assertTrue(project.filmstrip_visible)

    def test_identity_tile_is_outside_order_and_filmstrip_state_persists(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl.core.state import APP_STATE
        from hydra_manga_tl.core.settings import SETTINGS
        from hydra_manga_tl.ui import WorkspaceScreen
        from hydra_manga_tl.project.workspace import WORKSPACE

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            previous = WORKSPACE.current
            previous_mode = SETTINGS.filmstrip_collapse_mode
            SETTINGS.filmstrip_collapse_mode = "current"
            project = MangaProject.create("Filmstrip", Path(folder) / "project")
            project.images = [
                ImageRecord("one", "one.png", "one.png"),
                ImageRecord("two", "two.png", "two.png"),
            ]
            WORKSPACE.current = project
            APP_STATE.set_project(project)
            screen = WorkspaceScreen()
            with patch.object(screen, "_load_image") as load_image, \
                    patch.object(screen, "_show_identity_tile_in_filmstrip", wraps=screen._show_identity_tile_in_filmstrip) as show_identity:
                screen.refresh(project)
            load_image.assert_not_called()
            self.assertEqual(["one", "two"], screen.filmstrip.ordered_ids())
            self.assertEqual("Hydra", screen.identity_tile.text())
            show_identity.assert_called_once()
            self.assertTrue(screen.identity_tile.isChecked())
            self.assertEqual(-1, screen.filmstrip.currentRow())
            self.assertEqual("Hydra identity", screen.image_label.text())
            self.assertIs(screen.identity_preview, screen.canvas_stack.currentWidget())
            self.assertEqual(-1, APP_STATE.selected_image)
            with patch.object(screen, "_load_image") as load_late_page:
                APP_STATE.select(0, 0)
                app.processEvents()
            load_late_page.assert_not_called()
            self.assertTrue(screen.identity_tile.isChecked())
            self.assertEqual(-1, screen.filmstrip.currentRow())
            self.assertEqual(-1, APP_STATE.selected_image)
            with patch.object(screen, "_load_image"):
                screen.filmstrip.setCurrentRow(0)
                app.processEvents()
            self.assertFalse(screen.identity_tile.isChecked())
            self.assertEqual(0, APP_STATE.selected_image)
            screen.filmstrip_section.toggle.click()
            app.processEvents()
            project.save()
            self.assertFalse(MangaProject.load(project.project_file).filmstrip_visible)
            self.assertEqual(0, APP_STATE.selected_image)
            WORKSPACE.current = None
            APP_STATE.reset()
            app.processEvents()
            self.assertEqual("", screen._filmstrip_project_id)
            WORKSPACE.current = project
            with patch.object(screen, "_load_image") as reopen_load:
                APP_STATE.set_project(project)
                app.processEvents()
            reopen_load.assert_not_called()
            self.assertTrue(screen.identity_tile.isChecked())
            self.assertEqual(-1, screen.filmstrip.currentRow())
            self.assertEqual(-1, APP_STATE.selected_image)
            screen.close()
            WORKSPACE.current = previous
            SETTINGS.filmstrip_collapse_mode = previous_mode
            APP_STATE.reset()

    def test_filmstrip_always_collapsed_overrides_without_project_save(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl import ui
        from hydra_manga_tl.core.state import APP_STATE
        from hydra_manga_tl.ui import WorkspaceScreen
        from hydra_manga_tl.project.workspace import WORKSPACE

        app = QApplication.instance() or QApplication([])
        old_mode = ui.SETTINGS.filmstrip_collapse_mode
        ui.SETTINGS.filmstrip_collapse_mode = "always_collapsed"
        with tempfile.TemporaryDirectory() as folder:
            previous = WORKSPACE.current
            project = MangaProject.create("Always", Path(folder) / "project")
            project.images = [ImageRecord("one", "one.png", "one.png")]
            project.filmstrip_visible = True
            project.save()
            WORKSPACE.current = project
            APP_STATE.set_project(project)
            screen = WorkspaceScreen()
            try:
                screen.refresh(project)
                self.assertFalse(screen.filmstrip_section.toggle.isChecked())
                self.assertTrue(MangaProject.load(project.project_file).filmstrip_visible)
                screen.filmstrip_section.toggle.click()
                app.processEvents()
                self.assertTrue(screen.filmstrip_section.toggle.isChecked())
                self.assertTrue(MangaProject.load(project.project_file).filmstrip_visible)
                screen.refresh(project)
                self.assertTrue(screen.filmstrip_section.toggle.isChecked())
            finally:
                screen.close()
                WORKSPACE.current = previous
                APP_STATE.reset()
                ui.SETTINGS.filmstrip_collapse_mode = old_mode

    def test_filmstrip_always_collapsed_resets_on_project_switch(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl import ui
        from hydra_manga_tl.core.state import APP_STATE
        from hydra_manga_tl.ui import WorkspaceScreen
        from hydra_manga_tl.project.workspace import WORKSPACE

        app = QApplication.instance() or QApplication([])
        old_mode = ui.SETTINGS.filmstrip_collapse_mode
        ui.SETTINGS.filmstrip_collapse_mode = "always_collapsed"
        with tempfile.TemporaryDirectory() as folder:
            previous = WORKSPACE.current
            first = MangaProject.create("First", Path(folder) / "first")
            second = MangaProject.create("Second", Path(folder) / "second")
            first.images = [ImageRecord("one", "one.png", "one.png")]
            second.images = [ImageRecord("two", "two.png", "two.png")]
            screen = WorkspaceScreen()
            try:
                WORKSPACE.current = first
                APP_STATE.set_project(first)
                screen.refresh(first)
                screen.filmstrip_section.toggle.click()
                app.processEvents()
                self.assertTrue(screen.filmstrip_section.toggle.isChecked())
                WORKSPACE.current = second
                APP_STATE.set_project(second)
                screen.refresh(second)
                self.assertFalse(screen.filmstrip_section.toggle.isChecked())
            finally:
                screen.close()
                WORKSPACE.current = previous
                APP_STATE.reset()
                ui.SETTINGS.filmstrip_collapse_mode = old_mode

    def test_filmstrip_current_mode_restores_saved_project_state(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl import ui
        from hydra_manga_tl.core.state import APP_STATE
        from hydra_manga_tl.ui import WorkspaceScreen
        from hydra_manga_tl.project.workspace import WORKSPACE

        app = QApplication.instance() or QApplication([])
        old_mode = ui.SETTINGS.filmstrip_collapse_mode
        ui.SETTINGS.filmstrip_collapse_mode = "current"
        with tempfile.TemporaryDirectory() as folder:
            previous = WORKSPACE.current
            project = MangaProject.create("Current", Path(folder) / "project")
            project.images = [ImageRecord("one", "one.png", "one.png")]
            project.filmstrip_visible = False
            WORKSPACE.current = project
            APP_STATE.set_project(project)
            screen = WorkspaceScreen()
            try:
                screen.refresh(project)
                self.assertFalse(screen.filmstrip_section.toggle.isChecked())
                screen.filmstrip_section.toggle.click()
                app.processEvents()
                self.assertTrue(MangaProject.load(project.project_file).filmstrip_visible)
                ui.SETTINGS.filmstrip_collapse_mode = "always_collapsed"
                screen._apply_filmstrip_collapse_preference(project, project.id)
                self.assertFalse(screen.filmstrip_section.toggle.isChecked())
                ui.SETTINGS.filmstrip_collapse_mode = "current"
                screen._apply_filmstrip_collapse_preference(project, project.id)
                self.assertTrue(screen.filmstrip_section.toggle.isChecked())
            finally:
                screen.close()
                WORKSPACE.current = previous
                APP_STATE.reset()
                ui.SETTINGS.filmstrip_collapse_mode = old_mode

    def test_translation_completion_preserves_identity_and_collapsed_filmstrip(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl import ui
        from hydra_manga_tl.core.state import APP_STATE
        from hydra_manga_tl.ui import WorkspaceScreen
        from hydra_manga_tl.project.workspace import WORKSPACE

        app = QApplication.instance() or QApplication([])
        old_mode = ui.SETTINGS.filmstrip_collapse_mode
        ui.SETTINGS.filmstrip_collapse_mode = "always_collapsed"
        with tempfile.TemporaryDirectory() as folder:
            previous = WORKSPACE.current
            project = MangaProject.create("Identity Finish", Path(folder) / "project")
            project.images = [ImageRecord("one", "one.png", "one.png")]
            project.filmstrip_visible = True
            WORKSPACE.current = project
            APP_STATE.set_project(project)
            screen = WorkspaceScreen()
            try:
                screen.refresh(project)
                self.assertTrue(screen.identity_tile.isChecked())
                self.assertEqual(-1, APP_STATE.selected_image)
                self.assertFalse(screen.filmstrip_section.toggle.isChecked())
                WORKSPACE._active_job_ids = ["one"]
                WORKSPACE._active_job_completed = 0
                WORKSPACE._on_image_finished("one", {"status": "ready"})
                APP_STATE.refresh_project()
                app.processEvents()
                self.assertTrue(screen.identity_tile.isChecked())
                self.assertEqual(-1, APP_STATE.selected_image)
                self.assertEqual(-1, screen.filmstrip.currentRow())
                self.assertFalse(screen.filmstrip_section.toggle.isChecked())
                self.assertEqual(-1, MangaProject.load(project.project_file).selected_image)
            finally:
                WORKSPACE._active_job_ids = []
                screen.close()
                WORKSPACE.current = previous
                APP_STATE.reset()
                ui.SETTINGS.filmstrip_collapse_mode = old_mode

    def test_frozen_asset_roots_include_internal_folder(self):
        from hydra_manga_tl.core import application

        with patch.object(application.sys, "executable", r"C:\App\Hydra Manga TL.exe"), \
                patch.object(application.sys, "_MEIPASS", r"C:\App\_internal", create=True):
            roots = application.MangaApplication._asset_roots()
        self.assertIn(Path(r"C:\App"), roots)
        self.assertIn(Path(r"C:\App\_internal"), roots)

    def test_source_asset_resolver_finds_landing_logo(self):
        from hydra_manga_tl.core.assets import find_asset

        logo = find_asset("logos", "mainlogo.png")
        self.assertIsNotNone(logo)
        self.assertTrue(logo.is_file())

    def test_region_type_normalization_and_renderer_routing(self):
        self.assertEqual(normalize_region_type(None), "dialogue")
        self.assertEqual(normalize_region_type("speech"), "dialogue")
        self.assertEqual(normalize_region_type("narration"), "dialogue")
        self.assertEqual(group_region_type({"bubble_type": "title"}), "title")
        self.assertTrue(is_title_like_region({"bubble_type": "sfx"}))
        self.assertEqual(renderer_for_region({"bubble_type": "dialogue"}), "dialogue")
        for kind in ("title", "sfx", "sign", "credit"):
            self.assertEqual(renderer_for_region({"bubble_type": kind}), "title")

    def test_auto_translation_toggle_keeps_region_type_independent(self):
        config = {"translate_title": False, "translate_sfx": True}
        self.assertFalse(_auto_translate_region_type("title", config))
        self.assertTrue(_auto_translate_region_type("sfx", config))
        self.assertTrue(_auto_translate_region_type("dialogue", config))
        self.assertEqual(normalize_region_type("title"), "title")

    def test_title_render_group_uses_text_layout_without_moving_source_mask(self):
        group = {
            "index": 1,
            "bubble_type": "title",
            "polygon": [[10, 10], [40, 10], [40, 40], [10, 40]],
            "source_polygons": [[[10, 10], [40, 10], [40, 40], [10, 40]]],
            "text_layout": {"x": 80, "y": 90, "width": 50, "height": 30},
        }
        routed = _title_render_group(group, (200, 200))
        self.assertEqual(routed["title_render_polygon"], [[80, 90], [130, 90], [130, 120], [80, 120]])
        self.assertEqual(routed["source_polygons"], group["source_polygons"])
        self.assertEqual(routed["renderable_type"], "title")

    def test_make_mask_prefers_explicit_cleanup_polygons(self):
        from hydra_manga_tl.phase.renderer import make_mask

        mask = make_mask((100, 100), [{
            "source_polygons": [[[0, 0], [90, 0], [90, 90], [0, 90]]],
            "mask_polygons": [[[10, 10], [20, 10], [20, 20], [10, 20]]],
        }], dilation=0)

        self.assertEqual(int(mask[15, 15]), 255)
        self.assertEqual(int(mask[70, 70]), 0)

    def test_title_glyph_mask_extracts_colored_glyph_components(self):
        from PIL import ImageDraw
        from hydra_manga_tl.title.mask_extractor import extract_title_glyph_mask

        image = Image.new("RGB", (180, 90), "white")
        draw = ImageDraw.Draw(image)
        for box in ((20, 25, 38, 58), (58, 25, 82, 58), (104, 25, 134, 58)):
            draw.rectangle(box, fill=(216, 27, 115))
            draw.rectangle((box[0] + 6, box[1], box[0] + 10, box[3]), fill=(255, 255, 255))
            draw.rectangle((box[0], box[1] + 22, box[2], box[1] + 26), fill=(75, 0, 40))
        group = {
            "source_polygons": [
                [[18, 22], [48, 22], [48, 63], [18, 63]],
                [[55, 22], [93, 22], [93, 63], [55, 63]],
                [[101, 22], [148, 22], [148, 63], [101, 63]],
            ],
        }

        result = extract_title_glyph_mask(image, group, image.size)

        self.assertTrue(result.accepted, result.report())
        self.assertEqual(result.method, "opencv-glyph")
        self.assertGreater(int(result.mask[30, 25]), 0)
        self.assertEqual(int(result.mask[10, 10]), 0)

    def test_title_glyph_mask_reports_reconstruction_candidates(self):
        from PIL import ImageDraw
        from hydra_manga_tl.title.mask_extractor import extract_title_glyph_mask

        image = Image.new("RGB", (180, 90), "white")
        draw = ImageDraw.Draw(image)
        for box in ((20, 25, 38, 58), (58, 25, 82, 58), (104, 25, 134, 58)):
            draw.rectangle(box, fill=(216, 27, 115))
            draw.rectangle((box[0] - 2, box[1] - 2, box[2] + 2, box[3] + 2), outline=(255, 255, 255), width=2)
        group = {
            "original_text": "title-candidates",
            "source_text_colors": [[216, 27, 115]],
            "style_profile": {"outline": {"width": 2}, "stroke": {"width": 1}},
            "source_polygons": [
                [[18, 22], [48, 22], [48, 63], [18, 63]],
                [[55, 22], [93, 22], [93, 63], [55, 63]],
                [[101, 22], [148, 22], [148, 63], [101, 63]],
            ],
        }

        result = extract_title_glyph_mask(image, group, image.size)
        report = result.report()
        names = {item["name"] for item in report["title_mask_candidates"]}

        self.assertTrue(result.accepted, report)
        self.assertEqual(result.method, "opencv-glyph")
        self.assertIn(report["title_mask_selected_candidate"], names)
        self.assertTrue({"fill-color", "outline-edge", "stroke-expanded", "row-component", "conservative-merged"}.issubset(names))
        self.assertGreaterEqual(report["title_mask_component_summary"]["candidate_count"], 5)

    def test_manual_title_mask_can_fill_complete_outlined_glyphs(self):
        from PIL import ImageDraw
        from hydra_manga_tl.title.mask_extractor import extract_title_glyph_mask

        image = Image.new("RGB", (180, 100), (120, 130, 140))
        draw = ImageDraw.Draw(image)
        for box in ((20, 22, 48, 76), (70, 22, 98, 76), (120, 22, 148, 76)):
            draw.rectangle(box, fill="white")
            draw.rectangle((box[0] + 5, box[1] + 5, box[2] - 5, box[3] - 5), fill=(216, 27, 115))
        group = {
            "manual": True,
            "render_mode": "art_text",
            "original_text": "outlined-title",
            "source_polygons": [[[15, 17], [153, 17], [153, 81], [15, 81]]],
        }

        result = extract_title_glyph_mask(image, group, image.size)

        self.assertTrue(result.accepted, result.report())
        self.assertIn(result.selected_candidate, {"outline-solid", "outline-complete", "complete-print"})
        self.assertGreater(int(result.mask[45, 34]), 0)
        self.assertEqual(int(result.mask[8, 8]), 0)

    def test_manual_complete_title_mask_allows_larger_page_coverage(self):
        from hydra_manga_tl.title.reconstruction.engine import _mask_validation

        mask = np.zeros((100, 100), dtype=np.uint8)
        mask[:22, :] = 255
        metadata = {"title_mask_selected_candidate": "outline-complete"}

        self.assertFalse(_mask_validation(mask, (100, 100), metadata)["passed"])
        self.assertTrue(_mask_validation(mask, (100, 100), metadata, max_coverage=0.24)["passed"])

    def test_title_glyph_mask_style_expansion_and_cache_reuse(self):
        from PIL import ImageDraw
        from hydra_manga_tl.title import mask_extractor

        mask_extractor._MASK_CACHE.clear()
        image = Image.new("RGB", (140, 80), "white")
        draw = ImageDraw.Draw(image)
        for box in ((18, 25, 36, 52), (54, 25, 72, 52), (90, 25, 108, 52)):
            draw.rectangle(box, fill=(216, 27, 115))
        group = {
            "project_id": "cache-test",
            "original_text": "cache",
            "source_text_colors": [[216, 27, 115]],
            "style_profile": {"outline": {"width": 4}, "stroke": {"width": 3}},
            "source_polygons": [
                [[15, 22], [40, 22], [40, 55], [15, 55]],
                [[51, 22], [76, 22], [76, 55], [51, 55]],
                [[87, 22], [112, 22], [112, 55], [87, 55]],
            ],
        }

        first = mask_extractor.extract_title_glyph_mask(image, group, image.size)
        second = mask_extractor.extract_title_glyph_mask(image, group, image.size)

        self.assertTrue(first.accepted, first.report())
        candidate_names = {item["name"] for item in first.report()["title_mask_candidates"]}
        self.assertIn(first.selected_candidate, candidate_names)
        self.assertIn("stroke-expanded", candidate_names)
        self.assertTrue(second.accepted, second.report())
        self.assertEqual(second.report()["title_mask_component_summary"]["cache"], "hit")

    def test_title_glyph_mask_component_validation_drops_oversized_art(self):
        from PIL import ImageDraw
        from hydra_manga_tl.title.mask_extractor import extract_title_glyph_mask

        image = Image.new("RGB", (220, 120), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((10, 10, 118, 108), fill=(218, 40, 120))
        for box in ((140, 30, 152, 62), (163, 30, 175, 62), (186, 30, 198, 62)):
            draw.rectangle(box, fill=(216, 27, 115))
        group = {
            "original_text": "drop-large",
            "source_text_colors": [[216, 27, 115]],
            "source_polygons": [
                [[8, 8], [120, 8], [120, 110], [8, 110]],
                [[137, 26], [155, 26], [155, 66], [137, 66]],
                [[160, 26], [178, 26], [178, 66], [160, 66]],
                [[183, 26], [201, 26], [201, 66], [183, 66]],
            ],
        }

        result = extract_title_glyph_mask(image, group, image.size)

        self.assertTrue(result.accepted, result.report())
        self.assertEqual(int(result.mask[50, 50]), 0)
        self.assertGreater(int(result.mask[36, 145]), 0)

    def test_title_glyph_mask_rejects_broad_title_box(self):
        from hydra_manga_tl.title.mask_extractor import extract_title_glyph_mask

        image = Image.new("RGB", (180, 90), "white")
        group = {"source_polygons": [[[10, 10], [170, 10], [170, 80], [10, 80]]]}

        result = extract_title_glyph_mask(image, group, image.size)

        self.assertFalse(result.accepted)
        self.assertEqual(result.warning, "broad_title_box_requires_glyph_evidence")

    def test_title_glyph_mask_accepts_explicit_manual_mask(self):
        from hydra_manga_tl.title.mask_extractor import extract_title_glyph_mask

        image = Image.new("RGB", (100, 100), "white")
        group = {
            "source_polygons": [[[0, 0], [90, 0], [90, 90], [0, 90]]],
            "mask_polygons": [[[10, 10], [20, 10], [20, 20], [10, 20]]],
        }

        result = extract_title_glyph_mask(image, group, image.size)

        self.assertTrue(result.accepted, result.report())
        self.assertEqual(result.method, "explicit-polygons")
        self.assertEqual(int(result.mask[15, 15]), 255)
        self.assertEqual(int(result.mask[70, 70]), 0)

    def test_phase3_skips_unsafe_title_cleanup_for_broad_box(self):
        from hydra_manga_tl.phase.phase3 import run as render_phase3

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            Image.new("RGB", (140, 100), "white").save(source)
            payload = {
                "project_id": "mask-test",
                "source": str(source),
                "source_language": "Japanese",
                "target_language": "English",
                "translation_groups": [{
                    "index": 1,
                    "type": "title",
                    "bubble_type": "title",
                    "renderable_type": "title",
                    "render_mode": "art_text",
                    "status": "translated",
                    "original_text": "題名",
                    "translated_text": "TITLE",
                    "polygon": [[10, 10], [130, 10], [130, 90], [10, 90]],
                    "source_polygons": [[[10, 10], [130, 10], [130, 90], [10, 90]]],
                    "title_render_polygon": [[20, 25], [120, 25], [120, 70], [20, 70]],
                    "style_profile": {
                        "fill": {"dominant_color": [255, 255, 255]},
                        "outline": {"color": [0, 0, 0], "width": 1},
                    },
                }],
            }
            input_path = root / "input_translated_en.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            output = root / "rendered"

            render_phase3(input_path, output)

            report = json.loads((output / "source_render.json").read_text(encoding="utf-8"))
            self.assertEqual(report["cleaning_method"], "title-glyph-mask-skipped")
            self.assertTrue(report["needs_title_mask_review"])
            self.assertEqual(report["title_mask_accepted_count"], 0)
            self.assertEqual(report["rendered_groups"][0]["title_mask_warning"], "broad_title_box_requires_glyph_evidence")
            self.assertIn("title_mask_candidates", report["rendered_groups"][0])
            self.assertIn("title_mask_selected_candidate", report["rendered_groups"][0])

    def test_title_background_plate_rejects_complex_line_art(self):
        from PIL import ImageDraw
        from hydra_manga_tl.title.background_plate import apply_title_background_plates

        image = Image.new("RGB", (220, 140), "white")
        draw = ImageDraw.Draw(image)
        for offset in range(-100, 240, 10):
            draw.line((offset, 0, offset + 160, 140), fill=(0, 0, 0), width=2)
            draw.line((offset, 140, offset + 160, 0), fill=(0, 0, 0), width=2)
        group = {
            "index": 1,
            "source_polygons": [[[45, 45], [175, 45], [175, 95], [45, 95]]],
        }

        result = apply_title_background_plates(image, [group], image.size)

        self.assertFalse(result.accepted)
        self.assertEqual(result.reports[0]["title_background_plate_warning"], "line_art_overlap_rejected")

    def test_title_reconstruction_provider_registry_defaults_to_opencv(self):
        from hydra_manga_tl.title.reconstruction import create_reconstruction_provider

        provider, warning = create_reconstruction_provider("")

        self.assertEqual(provider.provider_id, "opencv")
        self.assertEqual(warning, "")
        self.assertTrue(provider.capabilities.supports_segmentation)
        self.assertFalse(provider.capabilities.supports_cleanup)

    def test_reconstruction_analysis_provider_defaults_to_none(self):
        from hydra_manga_tl.title.reconstruction import create_reconstruction_analysis_provider

        provider, warning = create_reconstruction_analysis_provider("")

        self.assertEqual(provider.provider_id, "none")
        self.assertEqual(warning, "")
        self.assertFalse(provider.capabilities.supports_mask_hints)

    def test_reconstruction_analysis_registers_ai_providers(self):
        from hydra_manga_tl.title.reconstruction import create_reconstruction_analysis_provider

        groq, groq_warning = create_reconstruction_analysis_provider("groq")
        gemini, gemini_warning = create_reconstruction_analysis_provider("gemini")

        self.assertEqual(groq.provider_id, "groq")
        self.assertEqual(gemini.provider_id, "gemini")
        self.assertEqual(groq_warning, "")
        self.assertEqual(gemini_warning, "")
        self.assertTrue(groq.capabilities.supports_mask_hints)
        self.assertTrue(gemini.capabilities.supports_background_analysis)

    def test_reconstruction_analysis_unknown_provider_falls_back_to_none(self):
        from hydra_manga_tl.title.reconstruction import reconstruct_title_group

        image = Image.new("RGB", (120, 80), "white")
        group = {"index": 1, "source_polygons": [[[10, 10], [110, 10], [110, 70], [10, 70]]]}

        _result, report = reconstruct_title_group(image, group, image.size, analysis_provider_id="qwen-vl")
        payload = report.to_dict()

        self.assertEqual(payload["reconstruction_analysis_provider"], "none")
        self.assertIn("unknown_analysis_provider:qwen-vl", payload["reconstruction_trace"][0]["warning"])

    def test_reconstruction_hint_validation_trust_levels(self):
        from hydra_manga_tl.title.reconstruction import (
            ReconstructionAnalysisCapabilities,
            ReconstructionAnalysisResult,
            build_reconstruction_request,
            validate_reconstruction_hints,
        )

        image = Image.new("RGB", (120, 80), "white")
        group = {"index": 1, "source_polygons": [[[20, 20], [80, 20], [80, 50], [20, 50]]]}
        request = build_reconstruction_request(image, group, image.size)
        analysis = ReconstructionAnalysisResult(
            provider="test",
            capabilities=ReconstructionAnalysisCapabilities(supports_geometry=True, supports_mask_hints=True),
            confidence=0.8,
            hints=[
                {"type": "mask_polygon", "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]], "confidence": 0.9},
                {"type": "hierarchy", "rank": 1, "confidence": 0.3},
                {"type": "mask_polygon", "polygon": [[25, 25], [70, 25], [70, 45], [25, 45]], "confidence": 0.9},
            ],
        )

        hints, ignored, used = validate_reconstruction_hints(analysis, request)

        self.assertEqual(hints[0]["trust_level"], 0)
        self.assertEqual(hints[1]["trust_level"], 1)
        self.assertEqual(hints[2]["trust_level"], 3)
        self.assertIn("hint_outside_title_region", ignored)
        self.assertIn("mask_polygon", used)

    def test_validated_ai_cleanup_polygon_becomes_safe_extractor_input(self):
        from hydra_manga_tl.title.reconstruction import (
            ReconstructionAnalysisCapabilities,
            ReconstructionAnalysisResult,
            apply_analysis_to_group,
            build_reconstruction_request,
            validate_reconstruction_hints,
        )

        image = Image.new("RGB", (120, 80), "white")
        group = {"index": 1, "source_polygons": [[[20, 20], [100, 20], [100, 60], [20, 60]]]}
        request = build_reconstruction_request(image, group, image.size)
        analysis = ReconstructionAnalysisResult(
            provider="groq",
            capabilities=ReconstructionAnalysisCapabilities(supports_geometry=True, supports_mask_hints=True),
            confidence=0.9,
            hints=[{"type": "cleanup_polygon", "polygon": [[35, 28], [70, 28], [70, 45], [35, 45]], "confidence": 0.92}],
        )

        hints, _ignored, _used = validate_reconstruction_hints(analysis, request)
        applied = apply_analysis_to_group(group, hints)

        self.assertIn("cleanup_polygon", applied)
        self.assertEqual(hints[0]["trust_level"], 4)
        self.assertEqual(group["cleanup_polygons"], [[[35, 28], [70, 28], [70, 45], [35, 45]]])

    def test_groq_reconstruction_analysis_parses_advisory_hints(self):
        from hydra_manga_tl.title.reconstruction import GroqReconstructionAnalysisProvider, build_reconstruction_request

        image = Image.new("RGB", (120, 80), "white")
        group = {"index": 1, "source_polygons": [[[20, 20], [100, 20], [100, 60], [20, 60]]]}
        request = build_reconstruction_request(image, group, image.size)
        response = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "confidence": 0.86,
                        "warnings": [],
                        "hints": [{"type": "background_risk", "confidence": 0.8, "reason": "simple_background"}],
                    })
                }
            }]
        }

        with patch("hydra_manga_tl.title.reconstruction.analysis._post_json", return_value=response) as post:
            provider = GroqReconstructionAnalysisProvider(api_key="test-key", model="vision-model")
            result = provider.analyze_reconstruction(request)

        self.assertEqual(result.provider, "groq")
        self.assertEqual(result.confidence, 0.86)
        self.assertEqual(result.hints[0]["type"], "background_risk")
        self.assertEqual(result.metadata["model"], "vision-model")
        self.assertTrue(post.called)

    def test_reconstruction_analysis_provider_cannot_mutate_group_state(self):
        from hydra_manga_tl.title.reconstruction import reconstruct_title_group
        from hydra_manga_tl.title.reconstruction.analysis import (
            RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY,
            AnalysisProviderRegistration,
            ReconstructionAnalysisCapabilities,
            ReconstructionAnalysisResult,
        )

        class MutatingAnalysisProvider:
            provider_id = "mutating"
            label = "Mutating"
            capabilities = ReconstructionAnalysisCapabilities(supports_geometry=True)

            def analyze_reconstruction(self, request):
                request.group["translated_text"] = "MUTATED"
                return ReconstructionAnalysisResult(provider=self.provider_id, capabilities=self.capabilities, confidence=0.9)

        previous = dict(RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY)
        RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY["mutating"] = AnalysisProviderRegistration("mutating", "Mutating", MutatingAnalysisProvider)
        try:
            image = Image.new("RGB", (120, 80), "white")
            group = {"index": 1, "translated_text": "ORIGINAL", "source_polygons": [[[10, 10], [110, 10], [110, 70], [10, 70]]]}
            reconstruct_title_group(image, group, image.size, analysis_provider_id="mutating")
        finally:
            RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY.clear()
            RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY.update(previous)

        self.assertEqual(group["translated_text"], "ORIGINAL")

    def test_reconstruction_analysis_character_overlap_forces_review(self):
        from hydra_manga_tl.title.reconstruction import reconstruct_title_group
        from hydra_manga_tl.title.reconstruction.analysis import (
            RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY,
            AnalysisProviderRegistration,
            ReconstructionAnalysisCapabilities,
            ReconstructionAnalysisResult,
        )

        class CharacterRiskAnalysisProvider:
            provider_id = "character-risk"
            label = "Character Risk"
            capabilities = ReconstructionAnalysisCapabilities(supports_cleanup_review=True)

            def analyze_reconstruction(self, request):
                return ReconstructionAnalysisResult(
                    provider=self.provider_id,
                    capabilities=self.capabilities,
                    confidence=0.9,
                    hints=[{"type": "character_overlap", "confidence": 0.9, "reason": "face_overlap"}],
                )

        previous = dict(RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY)
        RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY["character-risk"] = AnalysisProviderRegistration("character-risk", "Character Risk", CharacterRiskAnalysisProvider)
        try:
            image = Image.new("RGB", (100, 80), "white")
            group = {
                "index": 1,
                "source_polygons": [[[10, 10], [60, 10], [60, 50], [10, 50]]],
                "mask_polygons": [[[20, 20], [35, 20], [35, 35], [20, 35]]],
            }
            _result, report = reconstruct_title_group(image, group, image.size, analysis_provider_id="character-risk")
            payload = report.to_dict()
        finally:
            RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY.clear()
            RECONSTRUCTION_ANALYSIS_PROVIDER_REGISTRY.update(previous)

        self.assertEqual(payload["title_reconstruction_status"], "review")
        self.assertEqual(payload["title_reconstruction_warning"], "analysis_review_required")
        self.assertIn("character_overlap", payload["ai_suggestions_used"])
        self.assertIn(4, payload["reconstruction_analysis_trust_levels"])

    def test_title_reconstruction_unknown_provider_falls_back_with_trace(self):
        from hydra_manga_tl.title.reconstruction import reconstruct_title_group

        image = Image.new("RGB", (180, 90), "white")
        group = {"index": 1, "source_polygons": [[[10, 10], [170, 10], [170, 80], [10, 80]]]}

        _result, report = reconstruct_title_group(image, group, image.size, provider_id="sam2")
        payload = report.to_dict()

        self.assertEqual(payload["title_reconstruction_provider"], "opencv")
        self.assertEqual(payload["title_reconstruction_status"], "review")
        provider_step = next(item for item in payload["reconstruction_trace"] if item["stage"] == "provider")
        self.assertEqual(provider_step["status"], "fallback")
        self.assertIn("unknown_provider:sam2", provider_step["warning"])

    def test_title_reconstruction_engine_rejects_unsafe_provider_cleaned_image(self):
        from hydra_manga_tl.title.reconstruction.engine import reconstruct_title_group
        from hydra_manga_tl.title.reconstruction.models import ProviderCapabilities, TitleReconstructionResult
        from hydra_manga_tl.title.reconstruction.providers import ProviderRegistration, RECONSTRUCTION_PROVIDER_REGISTRY

        class UnsafeCleanedProvider:
            provider_id = "unsafe-cleaned"
            label = "Unsafe Cleaned"
            capabilities = ProviderCapabilities(
                supports_segmentation=True,
                supports_cleanup=True,
                supports_confidence=True,
                supports_validation=False,
            )

            def reconstruct_title(self, image, group, image_size):
                mask = np.zeros((image_size[1], image_size[0]), dtype=np.uint8)
                mask[20:30, 20:30] = 255
                cleaned = Image.new("RGB", image_size, "red")
                return TitleReconstructionResult(
                    provider=self.provider_id,
                    capabilities=self.capabilities,
                    mask=mask,
                    cleaned_image=cleaned,
                    confidence=0.9,
                    mask_quality=0.9,
                    cleanup_quality=0.9,
                    method="provider-cleaned-image",
                    metadata={"title_mask_method": "fake", "title_mask_confidence": 0.9, "title_mask_coverage": 0.01, "title_mask_warning": ""},
                )

        previous = dict(RECONSTRUCTION_PROVIDER_REGISTRY)
        RECONSTRUCTION_PROVIDER_REGISTRY["unsafe-cleaned"] = ProviderRegistration("unsafe-cleaned", "Unsafe Cleaned", UnsafeCleanedProvider)
        try:
            image = Image.new("RGB", (100, 80), "white")
            group = {"index": 1, "source_polygons": [[[18, 18], [34, 18], [34, 34], [18, 34]]]}
            _result, report = reconstruct_title_group(image, group, image.size, provider_id="unsafe-cleaned")
            payload = report.to_dict()
        finally:
            RECONSTRUCTION_PROVIDER_REGISTRY.clear()
            RECONSTRUCTION_PROVIDER_REGISTRY.update(previous)

        self.assertEqual(payload["title_reconstruction_status"], "accepted")
        self.assertEqual(payload["validation"]["provider_cleaned_image"]["reason"], "cleaned_changed_unmasked_art")
        self.assertNotEqual(payload["cleanup_method"], "provider-cleaned-image")

    def test_title_cleanup_validation_uses_bounded_chunks(self):
        from hydra_manga_tl.title.reconstruction.engine import _cleaned_validation

        original = Image.new("RGB", (900, 700), "white")
        cleaned = original.copy()
        mask = np.zeros((700, 900), dtype=np.uint8)
        mask[100:300, 200:500] = 255
        cleaned.paste((0, 0, 0), (200, 100, 500, 300))

        report = _cleaned_validation(original, cleaned, mask)

        self.assertTrue(report["passed"])
        self.assertEqual(report["outside_delta"], 0.0)
        original.close()
        cleaned.close()

    def test_title_reconstruction_cache_reuses_accepted_masks_only(self):
        from PIL import ImageDraw
        from hydra_manga_tl.title.reconstruction import clear_reconstruction_cache, reconstruct_title_group

        clear_reconstruction_cache()
        image = Image.new("RGB", (180, 90), "white")
        draw = ImageDraw.Draw(image)
        for box in ((20, 25, 38, 58), (58, 25, 82, 58), (104, 25, 134, 58)):
            draw.rectangle(box, fill=(216, 27, 115))
        accepted_group = {
            "index": 1,
            "original_text": "cache-ok",
            "source_polygons": [
                [[18, 22], [48, 22], [48, 63], [18, 63]],
                [[55, 22], [93, 22], [93, 63], [55, 63]],
                [[101, 22], [148, 22], [148, 63], [101, 63]],
            ],
        }
        rejected_group = {
            "index": 2,
            "original_text": "cache-review",
            "source_polygons": [[[10, 10], [170, 10], [170, 80], [10, 80]]],
        }

        reconstruct_title_group(image, accepted_group, image.size)
        _accepted_result, accepted_report = reconstruct_title_group(image, accepted_group, image.size)
        reconstruct_title_group(image, rejected_group, image.size)
        _rejected_result, rejected_report = reconstruct_title_group(image, rejected_group, image.size)

        self.assertEqual(accepted_report.to_dict()["title_reconstruction_cache"], "hit")
        self.assertNotIn("title_reconstruction_cache", rejected_report.to_dict())

    def test_workspace_title_region_creation_queues_title_ocr_translation(self):
        from hydra_manga_tl.project.model import MangaProject
        from hydra_manga_tl.project.workspace import WorkspaceManager

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            Image.new("RGB", (120, 80), "white").save(source)
            project = MangaProject.create("Title UI", root / "project")
            project.add_sources([(source, source.name)])
            project.save()
            manager = WorkspaceManager()
            manager.current = project

            with patch.object(manager.manual_service, "submit", return_value=True) as submit:
                created = manager.request_title_region(0, [[10, 10], [90, 10], [90, 50], [10, 50]])

        self.assertTrue(created)
        self.assertEqual(len(project.images[0].manual_regions), 0)
        request = submit.call_args.args[0]
        self.assertTrue(str(request["request_id"]).startswith("title:"))
        self.assertEqual(request["bubble_type"], "title")
        self.assertEqual(request["render_mode"], "art_text")
        self.assertEqual(request["title_composition"], {})
        self.assertEqual(request["title_reconstruction"], {"manual_reconstruction": True})
        self.assertEqual(request["rect"], [10, 10, 90, 50])

    def test_manual_ocr_result_can_create_title_reconstruction_region(self):
        from hydra_manga_tl.project.model import MangaProject
        from hydra_manga_tl.project.workspace import WorkspaceManager

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            Image.new("RGB", (120, 80), "white").save(source)
            project = MangaProject.create("Title OCR", root / "project")
            project.add_sources([(source, source.name)])
            project.save()
            manager = WorkspaceManager()
            manager.current = project
            result = {
                "request_id": "title:test",
                "project_id": project.id,
                "image_id": project.images[0].id,
                "image_index": 0,
                "id": "title-manual",
                "rect": [10, 10, 90, 50],
                "polygon": [[10, 10], [90, 10], [90, 50], [10, 50]],
                "source_polygons": [[[10, 10], [90, 10], [90, 50], [10, 50]]],
                "original_text": "タイトル",
                "source_member_texts": ["タイトル"],
                "translated_text": "TITLE",
                "ocr_confidence": 0.91,
                "source_language": "Japanese",
                "direction": "horizontal-ltr",
                "status": "translated",
                "review_reasons": [],
                "suppressed_auto_group_indices": [],
                "bubble_type": "title",
                "render_mode": "art_text",
                "title_composition": {},
                "title_reconstruction": {
                    "manual_reconstruction": True,
                    "cleanup_polygons": [[[12, 12], [88, 12], [88, 48], [12, 48]]],
                },
                "style_profile": None,
            }

            with patch.object(manager, "_start_manual_region_render") as render:
                manager._on_manual_region_succeeded(result)
            payload = manager.effective_translation_payload(0)

        self.assertEqual(len(project.images[0].manual_regions), 1)
        manual = project.images[0].manual_regions[0]
        self.assertEqual(manual.original_text, "タイトル")
        self.assertEqual(manual.translated_text, "TITLE")
        self.assertEqual(manual.bubble_type, "title")
        self.assertEqual(manual.render_mode, "art_text")
        self.assertTrue(manual.title_reconstruction["manual_reconstruction"])
        self.assertTrue(render.called)
        group = payload["translation_groups"][0]
        self.assertEqual(group["original_text"], "タイトル")
        self.assertEqual(group["translated_text"], "TITLE")
        self.assertEqual(group["bubble_type"], "title")
        self.assertEqual(group["render_mode"], "art_text")
        self.assertEqual(group["source_polygons"], result["source_polygons"])
        self.assertEqual(group["cleanup_polygons"], result["title_reconstruction"]["cleanup_polygons"])

    def test_manual_title_without_translation_is_still_reconstruction_eligible(self):
        from hydra_manga_tl.phase.phase3 import should_replace

        group = {
            "manual": True,
            "bubble_type": "title",
            "render_mode": "art_text",
            "status": "review",
            "translated_text": "",
            "review_reasons": ["title_reconstruction_manual_text_required"],
        }

        replace, reason = should_replace(group, "Japanese", "complete")

        self.assertTrue(replace)
        self.assertIsNone(reason)

    def test_manual_title_broad_box_uses_glyph_evidence_instead_of_auto_rejecting(self):
        from PIL import ImageDraw
        from hydra_manga_tl.title.mask_extractor import extract_title_glyph_mask

        image = Image.new("RGB", (160, 100), "white")
        draw = ImageDraw.Draw(image)
        for box in ((30, 20, 48, 70), (65, 20, 86, 70), (105, 20, 128, 70)):
            draw.rectangle(box, fill=(220, 30, 120))
        group = {
            "manual": True,
            "bubble_type": "title",
            "render_mode": "art_text",
            "source_text_colors": [[220, 30, 120]],
            "source_polygons": [[[20, 10], [140, 10], [140, 85], [20, 85]]],
        }

        result = extract_title_glyph_mask(image, group, image.size)

        self.assertTrue(result.accepted, result.report())
        self.assertNotEqual(result.warning, "broad_title_box_requires_glyph_evidence")

    def test_region_type_dropdown_only_shows_title_for_title_blocks(self):
        from PySide6.QtWidgets import QApplication, QComboBox
        from hydra_manga_tl.ui import WorkspaceScreen

        app = QApplication.instance() or QApplication([])
        del app
        screen = WorkspaceScreen.__new__(WorkspaceScreen)
        screen.bubble_type = QComboBox()

        WorkspaceScreen._sync_bubble_type_options(screen, "dialogue")
        dialogue_values = [screen.bubble_type.itemData(index) for index in range(screen.bubble_type.count())]
        WorkspaceScreen._sync_bubble_type_options(screen, "title")
        title_values = [screen.bubble_type.itemData(index) for index in range(screen.bubble_type.count())]

        self.assertNotIn("title", dialogue_values)
        self.assertIn("title", title_values)
        self.assertEqual(screen.bubble_type.currentData(), "title")

    def test_phase3_uses_background_plate_when_glyph_mask_is_skipped_on_simple_background(self):
        from PIL import ImageDraw
        from hydra_manga_tl.phase.phase3 import run as render_phase3

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            image = Image.new("RGB", (300, 180), "white")
            pixels = image.load()
            for y in range(image.height):
                for x in range(image.width):
                    pixels[x, y] = (120 + y // 6, 190 + y // 20, 230)
            draw = ImageDraw.Draw(image)
            draw.rectangle((70, 64, 190, 82), fill=(220, 40, 120))
            draw.rectangle((92, 88, 230, 106), fill=(220, 40, 120))
            image.save(source)
            payload = {
                "project_id": "plate-test",
                "source": str(source),
                "source_language": "Japanese",
                "target_language": "English",
                "translation_groups": [{
                    "index": 1,
                    "type": "title",
                    "bubble_type": "title",
                    "renderable_type": "title",
                    "render_mode": "art_text",
                    "status": "translated",
                    "original_text": "題名",
                    "translated_text": "TITLE",
                    "polygon": [[40, 50], [240, 50], [240, 120], [40, 120]],
                    "source_polygons": [[[40, 50], [240, 50], [240, 120], [40, 120]]],
                    "title_render_polygon": [[55, 55], [235, 55], [235, 115], [55, 115]],
                    "style_profile": {
                        "fill": {"dominant_color": [255, 255, 255]},
                        "outline": {"color": [0, 0, 0], "width": 1},
                    },
                }],
            }
            input_path = root / "input_translated_en.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            output = root / "rendered"

            render_phase3(input_path, output)

            report = json.loads((output / "source_render.json").read_text(encoding="utf-8"))
            group_report = report["rendered_groups"][0]
            self.assertEqual(report["cleaning_method"], "background-plate-fallback")
            self.assertEqual(report["title_background_plate_accepted_count"], 1)
            self.assertFalse(report["needs_title_mask_review"])
            self.assertEqual(group_report["title_background_plate_method"], "local-gradient-plate")
            self.assertGreater(group_report["title_background_plate_confidence"], 0.55)
            self.assertEqual(group_report["title_reconstruction_status"], "accepted")
            self.assertIn("reconstruction_trace", group_report)
            self.assertEqual(group_report["reconstruction_analysis_provider"], "none")
            self.assertEqual(group_report["ai_suggestions_used"], [])
            self.assertIn("reconstruction_history", group_report)

    def test_phase3_report_includes_title_mask_candidate_metadata(self):
        from PIL import ImageDraw
        from hydra_manga_tl.phase.phase3 import run as render_phase3

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            image = Image.new("RGB", (180, 90), "white")
            draw = ImageDraw.Draw(image)
            for box in ((20, 25, 38, 58), (58, 25, 82, 58), (104, 25, 134, 58)):
                draw.rectangle(box, fill=(216, 27, 115))
            image.save(source)
            payload = {
                "project_id": "mask-report-test",
                "source": str(source),
                "source_language": "Japanese",
                "target_language": "English",
                "translation_groups": [{
                    "index": 1,
                    "type": "title",
                    "bubble_type": "title",
                    "renderable_type": "title",
                    "render_mode": "art_text",
                    "status": "translated",
                    "original_text": "題名",
                    "translated_text": "TITLE",
                    "polygon": [[15, 20], [150, 20], [150, 65], [15, 65]],
                    "source_polygons": [
                        [[18, 22], [48, 22], [48, 63], [18, 63]],
                        [[55, 22], [93, 22], [93, 63], [55, 63]],
                        [[101, 22], [148, 22], [148, 63], [101, 63]],
                    ],
                    "title_render_polygon": [[20, 20], [150, 20], [150, 70], [20, 70]],
                    "source_text_colors": [[216, 27, 115]],
                    "style_profile": {
                        "fill": {"dominant_color": [216, 27, 115]},
                        "outline": {"color": [255, 255, 255], "width": 1},
                    },
                }],
            }
            input_path = root / "input_translated_en.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            output = root / "rendered"

            render_phase3(input_path, output)

            report = json.loads((output / "source_render.json").read_text(encoding="utf-8"))
            group_report = report["rendered_groups"][0]
            self.assertGreaterEqual(report["title_mask_accepted_count"], 1)
            self.assertIn(group_report["title_mask_selected_candidate"], {item["name"] for item in group_report["title_mask_candidates"]})
            self.assertGreaterEqual(group_report["title_mask_component_summary"]["candidate_count"], 5)
            self.assertEqual(group_report["title_reconstruction_provider"], "opencv")
            self.assertEqual(group_report["title_reconstruction_status"], "accepted")
            self.assertIn("reconstruction_trace", group_report)
            self.assertEqual(report["reconstruction_analysis_provider"], "none")
            self.assertEqual(group_report["reconstruction_analysis_provider"], "none")
            self.assertIn("reconstruction_analysis_capabilities", group_report)
            self.assertEqual(group_report["reconstruction_analysis_hints"], [])
            self.assertEqual(group_report["ai_suggestions_used"], [])
            self.assertEqual(group_report["reconstruction_history"][-1]["title_reconstruction_status"], "accepted")

    def test_lazy_style_extraction_is_saved_and_reused_from_payload(self):
        from hydra_manga_tl.phase.art_inpaint import render_art_text
        from hydra_manga_tl.title.utils import PATHS as TITLE_PATHS

        image = Image.new("RGB", (160, 120), "white")
        mask = Image.new("L", (160, 120), 255)
        profile = TitleStyleProfile(
            fill=FillProfile(dominant_color=(10, 20, 30)),
            outline=OutlineProfile(color=(240, 240, 240), width=2.0),
        )
        group = {
            "index": "manual:1",
            "bubble_type": "title",
            "polygon": [[20, 20], [140, 20], [140, 80], [20, 80]],
            "source_polygons": [[[20, 20], [140, 20], [140, 80], [20, 80]]],
            "original_text": "題名",
            "translated_text": "Title",
        }
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(TITLE_PATHS, "title_style_cache", Path(folder) / "title_style_cache"), \
                    patch("hydra_manga_tl.phase.art_inpaint.extract_title_style", return_value=profile) as extract, \
                    patch("hydra_manga_tl.phase.art_inpaint.sample_style", return_value={"fill": (1, 1, 1), "stroke": (2, 2, 2), "accent": (3, 3, 3)}):
                first = render_art_text(image.copy(), image, mask, dict(group), project_id="project")
                self.assertEqual(extract.call_count, 1)
                self.assertIn("title_composition", first)
                self.assertIn("style_profile", first)
                second_group = dict(group)
                second_group["style_profile"] = first["style_profile"]
                render_art_text(image.copy(), image, mask, second_group, project_id="project")
                self.assertEqual(extract.call_count, 1)

    def test_title_style_profile_v2_migrates_old_payloads(self):
        profile = TitleStyleProfile.from_dict({
            "fill": {"dominant_color": "#d81b73"},
            "outline": {"color": [255, 255, 255], "width": 2},
        })

        self.assertEqual(profile.version, 1)
        self.assertEqual(profile.fill.dominant_color, (216, 27, 115))
        self.assertIsNone(profile.stroke)
        upgraded = TitleStyleProfile.from_dict({**profile.to_dict(), "stroke": {"color": "#4b0028", "width": 3}})
        self.assertEqual(upgraded.stroke.color, (75, 0, 40))

    def test_title_composition_splits_polygons_and_detects_hierarchy(self):
        group = {
            "index": 7,
            "translated_text": "THE BANISHED ENCHANTER",
            "source_member_texts": ["THE", "BANISHED"],
            "style_profile": {"fill": {"dominant_color": [10, 20, 30]}, "outline": {"color": [255, 255, 255], "width": 2}},
        }
        polygons = [
            [[10, 10], [60, 10], [60, 30], [10, 30]],
            [[10, 40], [150, 40], [150, 110], [10, 110]],
        ]
        composition = TitleComposition.from_group(group, ["THE", "BANISHED ENCHANTER"], polygons)

        self.assertEqual(len(composition.layers), 2)
        self.assertEqual(composition.hierarchy[0], "layer-2")
        self.assertEqual(composition.layers[1].role, "main")
        self.assertEqual([item["id"] for item in composition.suggestions], ["source", "stacked", "compact"])

    def test_title_composition_uses_source_text_colors_per_layer(self):
        group = {
            "index": 8,
            "translated_text": "PINK WHITE",
            "source_member_texts": ["桃", "白"],
            "source_text_colors": [[216, 24, 120], [240, 240, 240]],
        }
        polygons = [
            [[10, 10], [90, 10], [90, 40], [10, 40]],
            [[10, 50], [90, 50], [90, 80], [10, 80]],
        ]

        composition = TitleComposition.from_group(group, ["PINK", "WHITE"], polygons)

        self.assertEqual(composition.layers[0].style_profile.fill.dominant_color, (216, 24, 120))
        self.assertEqual(composition.layers[1].style_profile.fill.dominant_color, (240, 240, 240))
        self.assertEqual(composition.layers[0].translated_text, "PINK")

    def test_manual_title_composition_is_built_from_ordered_source_segments(self):
        right_polygon = [[60, 5], [78, 5], [78, 65], [60, 65]]
        left_polygon = [[15, 10], [33, 10], [33, 70], [15, 70]]
        source_regions = [
            {"text": "赤文字", "polygon": left_polygon},
            {"text": "青文字", "polygon": right_polygon},
        ]

        members, polygons = _ordered_title_members(
            source_regions,
            [2, 1],
            [[0, 0], [90, 0], [90, 80], [0, 80]],
        )
        composition = _generic_title_composition(
            "manual-title",
            members,
            polygons,
            ["Blue title", "Red subtitle"],
            None,
        )

        self.assertEqual([right_polygon, left_polygon], composition["source_polygons"])
        self.assertEqual(
            ["BLUE TITLE", "RED SUBTITLE"],
            [layer["translated_text"] for layer in composition["layers"]],
        )
        self.assertTrue(
            all(layer["style_profile"]["fill"] is None for layer in composition["layers"]),
        )

    def test_pipeline_source_text_color_samples_ocr_polygon(self):
        from PIL import ImageDraw
        from hydra_manga_tl.phase.pipeline import _source_text_color

        image = Image.new("RGB", (80, 50), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((20, 10, 55, 35), fill=(216, 27, 115))

        color = _source_text_color(image, [[18, 8], [58, 8], [58, 38], [18, 38]])

        self.assertEqual(color, [216, 24, 96])

    def test_title_style_extractor_separates_outer_stroke(self):
        from PIL import ImageDraw
        from hydra_manga_tl.title.models import TitleObject, TitleRenderSettings
        from hydra_manga_tl.title.style_extractor import extract_title_style

        image = Image.new("RGB", (120, 80), "white")
        mask = Image.new("L", (120, 80), 0)
        draw = ImageDraw.Draw(image)
        mask_draw = ImageDraw.Draw(mask)
        draw.rectangle((24, 14, 96, 66), fill=(75, 0, 40))
        draw.rectangle((30, 20, 90, 60), fill=(255, 255, 255))
        draw.rectangle((38, 28, 82, 52), fill=(216, 27, 115))
        mask_draw.rectangle((30, 20, 90, 60), fill=255)
        title = TitleObject(
            id="title",
            polygon=[[30, 20], [90, 20], [90, 60], [30, 60]],
            original_text="TITLE",
            translated_text="TITLE",
            render_settings=TitleRenderSettings(),
        )

        profile = extract_title_style(image, mask, title)

        self.assertEqual(profile.version, 2)
        self.assertIsNotNone(profile.stroke)
        self.assertEqual(profile.stroke.color, (72, 0, 24))
        self.assertIn("stroke", profile.confidence)

    def test_pyinstaller_bundles_qt_text_to_speech_plugins(self):
        source = (Path(__file__).resolve().parents[1] / "HydraMangaTL.spec").read_text(encoding="utf-8")
        self.assertIn('"texttospeech"', source)

    def test_pyinstaller_embeds_application_ico_resource(self):
        source = (Path(__file__).resolve().parents[1] / "HydraMangaTL.spec").read_text(encoding="utf-8")
        self.assertIn('icon=str(PROJECT_ROOT / "assets" / "icons" / "app.ico")', source)

    def test_runtime_icon_resolver_prefers_ico(self):
        from hydra_manga_tl.core import application

        icon_path = application.MangaApplication._brand_icon_file()
        self.assertIsNotNone(icon_path)
        self.assertEqual(icon_path.name, "app.ico")

    def test_retry_budgets_are_hard_limits(self):
        regions = [_region("x", 0.1 + index * 0.01, 10 + index * 22) for index in range(20)]
        with tempfile.TemporaryDirectory() as folder:
            image = self.make_image(folder)
            fast = _RetryEngine(regions)
            balanced = _RetryEngine(regions)
            maximum = _RetryEngine(regions)
            SmartOCRManager(fast).analyze_page(image, quality="Fast")
            SmartOCRManager(balanced).analyze_page(image, quality="Balanced")
            SmartOCRManager(maximum).analyze_page(image, quality="Maximum")
        self.assertEqual(len(fast.selection_calls), 0)
        self.assertEqual(len(balanced.selection_calls), 1)
        self.assertEqual(len(maximum.selection_calls), 3)

    def test_retry_order_starts_with_worst_candidate(self):
        regions = [_region("かな", 0.65, 10), _region("かな", 0.10, 300)]
        with tempfile.TemporaryDirectory() as folder:
            image = self.make_image(folder)
            engine = _RetryEngine(regions)
            SmartOCRManager(engine).analyze_page(image, quality="Balanced")
        self.assertGreater(engine.selection_calls[0][0], 200)

    def test_balanced_defers_extra_uncertain_regions_to_review(self):
        regions = [_region("414", 0.20, 10), _region("515", 0.25, 200)]
        with tempfile.TemporaryDirectory() as folder:
            image = self.make_image(folder)
            engine = _RetryEngine(regions)
            managed = SmartOCRManager(engine).analyze_page(image, quality="Balanced")
        self.assertEqual(len(engine.selection_calls), 1)
        self.assertTrue(any(
            reason.startswith("retry_deferred:")
            for region in managed.final_regions
            for reason in region.get("ocr_review_reasons", [])
        ))
        self.assertTrue(managed.review_queue)

    def test_balanced_defers_oversized_retry_crop(self):
        large = TextRegion(
            "HELLO",
            0.90,
            [[50, 20], [400, 20], [400, 250], [50, 250]],
        )
        with tempfile.TemporaryDirectory() as folder:
            image = self.make_image(folder)
            engine = _RetryEngine([large])
            managed = SmartOCRManager(engine).analyze_page(image, quality="Balanced")
        self.assertEqual(engine.selection_calls, [])
        self.assertIn(
            "retry_deferred:oversized_crop",
            managed.final_regions[0]["ocr_review_reasons"],
        )

    def test_focused_selection_uses_one_color_prediction(self):
        class Prediction:
            json = {"res": {
                "rec_texts": ["待て"],
                "rec_scores": [0.9],
                "rec_polys": [[[2, 2], [42, 2], [42, 42], [2, 42]]],
            }}

        class NativeEngine:
            def __init__(self):
                self.paths = []

            def predict(self, path):
                self.paths.append(path)
                return [Prediction()]

        with tempfile.TemporaryDirectory() as folder:
            image = self.make_image(folder)
            native = NativeEngine()
            engine = PaddleOCREngine(["japan"])
            with patch.object(engine, "_engine", return_value=native):
                result = engine.analyze_selection(image, [10, 10, 80, 80], preferred_language="japan")
        self.assertEqual(len(native.paths), 1)
        self.assertTrue(native.paths[0].endswith("color2.png"))
        self.assertEqual(result.metadata["prediction_count"], 1)
        self.assertEqual(result.metadata["selection_variant"], "color2")

    def test_stale_project_status_recovers_to_partial_with_ocr_checkpoint(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project = MangaProject("project", "Recovery", str(root), images=[
                ImageRecord("page-1", str(root / "source.png"), "source.png", status="OCR"),
                ImageRecord("page-2", str(root / "source2.png"), "source2.png", status="rendering"),
            ])
            project.save()
            (project.artifacts / "page-1_ocr.json").write_text("{}", encoding="utf-8")
            WorkspaceManager._recover_interrupted_project(project)
            self.assertEqual(project.images[0].status, "partial")
            self.assertEqual(project.images[1].status, "queued")

    def test_manifest_done_page_is_not_reprocessed_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            source.write_bytes(b"source")
            project = MangaProject("project", "Recovery", str(root), images=[
                ImageRecord("page-1", str(source), "source.png", status="rendering"),
            ])
            project.save()
            translation = project.artifacts / "page-1_translated_en.json"
            translation.write_text(json.dumps({"translation_groups": [], "ai_review": {"issue_count": 0}}), encoding="utf-8")
            render_dir = project.artifacts / "page-1"
            render_dir.mkdir(parents=True)
            (render_dir / "source_translated_en.png").write_bytes(b"rendered")
            manifest = JobManifest(project.artifacts / "chapter_job_manifest.json")
            manifest.ensure_page("page-1", str(source))
            manifest.mark("page-1", "done", stage="review")
            WorkspaceManager._recover_interrupted_project(project)
        self.assertEqual(project.images[0].status, "ready")

    def test_translated_export_includes_untranslated_pages_in_order(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            export_dir = root / "export"
            records = []
            for index in range(1, 4):
                source = root / f"{index:03d}.png"
                source.write_bytes(f"source-{index}".encode("ascii"))
                records.append(ImageRecord(f"page-{index}", str(source), f"{index:03d}.png"))

            rendered_dir = root / "artifacts" / "page-1"
            rendered_dir.mkdir(parents=True)
            rendered = rendered_dir / "001_translated_en.png"
            rendered.write_bytes(b"translated-1")
            records[0].rendered_image = str(rendered)

            rendered_dir = root / "artifacts" / "page-3"
            rendered_dir.mkdir(parents=True)
            rendered = rendered_dir / "003_translated_en.png"
            rendered.write_bytes(b"translated-3")
            records[2].rendered_image = str(rendered)

            project = MangaProject("project", "Export", str(root), images=records)
            count = export_images(project, export_dir)
            archive_path = export_archive(project, root / "exported-strip")

            self.assertEqual(count, 3)
            self.assertEqual((export_dir / "001.png").read_bytes(), b"translated-1")
            self.assertEqual((export_dir / "002.png").read_bytes(), b"source-2")
            self.assertEqual((export_dir / "003.png").read_bytes(), b"translated-3")
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(archive.namelist(), ["001.png", "002.png", "003.png"])
                self.assertEqual(archive.read("002.png"), b"source-2")

    def test_manifest_recovers_stale_state(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = JobManifest(Path(folder) / "manifest.json")
            manifest.ensure_page("page", "source.png")
            manifest.mark("page", "translating", stage="OCR")
            recovered = manifest.recover_stale()
        self.assertEqual(recovered, {"page": "partial"})

    def test_default_settings_disable_debug_artifacts(self):
        self.assertFalse(AppSettings().debug_artifacts_enabled)

    def test_default_title_reconstruction_shortcut_is_ctrl_f(self):
        self.assertEqual(AppSettings().title_reconstruction_shortcut, "Ctrl+F")

    def test_default_filmstrip_collapse_mode_is_current(self):
        self.assertEqual(AppSettings().filmstrip_collapse_mode, "current")

    def test_default_app_data_root_uses_local_app_data(self):
        self.assertEqual(
            AppSettings().app_data_root,
            str(AppPaths.default_root().resolve()),
        )

    def test_app_data_root_reconfigures_loaded_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            default_paths = AppPaths(base / "default")
            custom_root = base / "custom-data"
            default_paths.root.mkdir(parents=True, exist_ok=True)
            default_paths.settings.write_text(
                json.dumps({"app_data_root": str(custom_root)}),
                encoding="utf-8",
            )
            loaded = AppSettings.load(default_paths)
        self.assertEqual(loaded.app_data_root, str(custom_root.resolve()))
        self.assertEqual(default_paths.root, custom_root.resolve())
        self.assertEqual(default_paths.projects, custom_root.resolve() / "projects")

    def test_settings_saved_to_explicit_paths_round_trip_that_root(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = AppPaths(Path(folder))
            AppSettings(filmstrip_collapse_mode="always_collapsed").save(paths)
            loaded = AppSettings.load(paths)
        self.assertEqual(loaded.app_data_root, str(Path(folder).resolve()))
        self.assertEqual(loaded.filmstrip_collapse_mode, "always_collapsed")

    def test_recent_project_data_delete_is_limited_to_app_projects(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            paths = AppPaths(root / "app-data")
            paths.initialize()
            manager = WorkspaceManager(paths=paths)

            owned_project = MangaProject(
                "owned",
                "Owned",
                str(paths.projects / "owned"),
            )
            owned_project.save()
            artifacts = owned_project.artifacts
            artifacts.mkdir(parents=True, exist_ok=True)
            (artifacts / "rendered.png").write_text("working output", encoding="utf-8")

            export_root = root / "exports" / "owned"
            export_root.mkdir(parents=True)
            (export_root / "translated.png").write_text("export", encoding="utf-8")

            external_project = MangaProject(
                "external",
                "External",
                str(root / "outside-project"),
            )
            external_project.save()

            self.assertEqual(
                manager.recent_project_data_root(owned_project.project_file),
                owned_project.root_path.resolve(),
            )
            self.assertIsNone(
                manager.recent_project_data_root(external_project.project_file)
            )

            deleted = manager.delete_recent_project_data(owned_project.project_file)

            self.assertEqual(deleted, owned_project.root_path.resolve())
            self.assertFalse(owned_project.root_path.exists())
            self.assertTrue(export_root.is_dir())
            self.assertTrue((export_root / "translated.png").is_file())
            self.assertTrue(external_project.root_path.is_dir())

    def test_filmstrip_collapse_mode_round_trips_through_settings_json(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = AppPaths(Path(folder))
            settings = AppSettings(filmstrip_collapse_mode="always_collapsed")
            settings.save(paths)
            self.assertEqual(AppSettings.load(paths).filmstrip_collapse_mode, "always_collapsed")

    def test_legacy_settings_default_filmstrip_collapse_mode_to_current(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = AppPaths(Path(folder))
            paths.root.mkdir(parents=True, exist_ok=True)
            paths.settings.write_text('{"debug_artifacts_enabled": true}', encoding="utf-8")
            loaded = AppSettings.load(paths)
        self.assertEqual(loaded.filmstrip_collapse_mode, "current")
        self.assertTrue(loaded.debug_artifacts_enabled)

    def test_invalid_filmstrip_collapse_mode_defaults_to_current(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = AppPaths(Path(folder))
            paths.root.mkdir(parents=True, exist_ok=True)
            paths.settings.write_text('{"filmstrip_collapse_mode": "locked"}', encoding="utf-8")
            loaded = AppSettings.load(paths)
        self.assertEqual(loaded.filmstrip_collapse_mode, "current")

    def test_settings_dialog_initializes_and_saves_filmstrip_collapse_mode(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl import ui
        from hydra_manga_tl.ui import dialogs as ui_dialogs
        from hydra_manga_tl.project.workspace import WORKSPACE

        QApplication.instance() or QApplication([])
        previous_project = WORKSPACE.current
        old_mode = ui.SETTINGS.filmstrip_collapse_mode
        old_root = ui.SETTINGS.app_data_root
        ui.SETTINGS.filmstrip_collapse_mode = "always_collapsed"
        WORKSPACE.current = None
        with tempfile.TemporaryDirectory() as folder:
            selected_root = Path(folder) / "hydra-data"
            old_paths_root = ui_dialogs.PATHS.root
            old_tm_path = ui_dialogs.TRANSLATION_MEMORY.path
            old_tm_legacy = ui_dialogs.TRANSLATION_MEMORY.legacy_path
            with patch.object(ui.CREDENTIALS, "get", return_value=""), \
                    patch.object(ui.CREDENTIALS, "set"), \
                    patch.object(ui.SETTINGS, "save") as save_settings, \
                    patch.object(ui.QMessageBox, "information"):
                dialog = ui.SettingsDialog()
                try:
                    self.assertEqual(dialog.filmstrip_collapse_mode.currentData(), "always_collapsed")
                    dialog.filmstrip_collapse_mode.setCurrentIndex(
                        dialog.filmstrip_collapse_mode.findData("current")
                    )
                    dialog.app_data_root.setText(str(selected_root))
                    dialog._save()
                    self.assertEqual(ui.SETTINGS.filmstrip_collapse_mode, "current")
                    self.assertEqual(ui.SETTINGS.app_data_root, str(selected_root.resolve()))
                    self.assertEqual(ui_dialogs.PATHS.root, selected_root.resolve())
                    self.assertEqual(ui_dialogs.TRANSLATION_MEMORY.path, selected_root.resolve() / "translation_memory.db")
                    save_settings.assert_called_once()
                finally:
                    dialog.close()
                    WORKSPACE.current = previous_project
                    ui.SETTINGS.filmstrip_collapse_mode = old_mode
                    ui.SETTINGS.app_data_root = old_root
                    ui_dialogs.PATHS.configure(old_paths_root)
                    ui_dialogs.TRANSLATION_MEMORY.configure(old_tm_path, legacy_path=old_tm_legacy)

    def test_ocr_service_uses_checkpoint_without_constructing_engine(self):
        with tempfile.TemporaryDirectory() as folder:
            checkpoint = Path(folder) / "ocr.json"
            cached = _result([_region("待て", 0.9, 10)])
            cached.metadata = {"manager": {"review_queue": [{
                "region_id": "r1",
                "reasons": ["retry_deferred:low_confidence"],
                "text": "待て",
                "confidence": 0.9,
            }]}}
            checkpoint.write_text(json.dumps(cached.to_dict()), encoding="utf-8")
            service = OCRService(("japan",), use_subprocess=False)
            with patch("hydra_manga_tl.ocr.service.get_ocr_engine") as get_engine:
                result = service.analyze_page(
                    Path(folder) / "unused.png",
                    preferred_language="japan",
                    quality="Balanced",
                    auto_language_fallback=False,
                    checkpoint_path=checkpoint,
                )
            get_engine.assert_not_called()
        self.assertTrue(result.cache_hit)
        self.assertEqual(result.checkpoint, "project")
        self.assertEqual(
            result.final_regions[0]["ocr_review_reasons"],
            ["retry_deferred:low_confidence"],
        )

    def test_worker_broken_pipe_is_contained_and_restarted(self):
        class AliveProcess:
            pid = 12345
            exitcode = -1073741819

            def is_alive(self):
                return True

        class BrokenConnection:
            def send(self, payload):
                raise BrokenPipeError("simulated native crash")

        client = OCRWorkerClient()
        client._process = AliveProcess()
        client._connection = BrokenConnection()
        with patch.object(client, "_restart") as restart:
            with self.assertRaises(OCRWorkerCrashed) as raised:
                client.analyze_page({"image_path": "page.png"})
        restart.assert_called_once()
        self.assertIn("BrokenPipeError", str(raised.exception))
        self.assertIn("pid=12345", str(raised.exception))
        self.assertIn("exitcode=-1073741819", str(raised.exception))

    def test_worker_warmup_image_does_not_require_freetype(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "warmup.png"
            _write_warmup_image(target)
            with Image.open(target) as image:
                self.assertEqual(image.size, (320, 140))
                self.assertLess(image.convert("L").getextrema()[0], 255)

    def test_worker_warmup_eof_is_contained_and_restarted(self):
        class AliveProcess:
            pid = 23456
            exitcode = -1073741819

            def is_alive(self):
                return True

        class EofDuringWarmupConnection:
            def poll(self, timeout):
                return True

            def recv(self):
                raise EOFError()

        client = OCRWorkerClient()
        client._process = AliveProcess()
        client._connection = EofDuringWarmupConnection()
        client._set_state("STARTING")
        with patch.object(client, "_restart") as restart:
            with self.assertRaises(OCRWorkerCrashed) as raised:
                client.analyze_selection({"image_path": "page.png", "rect": [0, 0, 10, 10]})
        restart.assert_called_once()
        self.assertIn("OCR worker exited during warm-up", str(raised.exception))
        self.assertIn("EOFError", str(raised.exception))
        self.assertIn("pid=23456", str(raised.exception))

    def test_worker_warmup_failure_response_is_contained_and_restarted(self):
        class AliveProcess:
            pid = 34567
            exitcode = None

            def is_alive(self):
                return True

        class FailedWarmupConnection:
            def poll(self, timeout):
                return True

            def recv(self):
                return {
                    "ok": False,
                    "state": "FAILED",
                    "error": "RuntimeError: Paddle failed to initialize",
                }

        client = OCRWorkerClient()
        client._process = AliveProcess()
        client._connection = FailedWarmupConnection()
        client._set_state("STARTING")
        with patch.object(client, "_restart") as restart:
            with self.assertRaises(OCRWorkerCrashed) as raised:
                client.analyze_selection({"image_path": "page.png", "rect": [0, 0, 10, 10]})
        restart.assert_called_once()
        self.assertIn("OCR worker failed during warm-up", str(raised.exception))
        self.assertIn("Paddle failed to initialize", str(raised.exception))

    def test_runtime_start_prewarms_worker_in_background(self):
        manager = OCRRuntimeManager()
        with patch.object(OCRWorkerClient, "start") as start, \
                patch.object(OCRWorkerClient, "ping", return_value=True) as ping:
            manager.start_warmup()
            self.assertIsNotNone(manager._warmup_thread)
            manager._warmup_thread.join(1.0)
        start.assert_called()
        ping.assert_called_once()

    def test_ocr_service_retries_subprocess_worker_crash_once(self):
        class FlakyWorker:
            restart_count = 0
            worker_rss_mb = 0.0

            def __init__(self):
                self.calls = 0

            def request(self, command, request):
                self.calls += 1
                if self.calls == 1:
                    raise OCRWorkerCrashed("first worker crash")
                self.command = command
                self.request = request
                managed = _result([_region("待て", 0.98, 10)])
                return {"ok": True, "ocr_result": managed.to_dict(), "final_regions": [{"text": "待て"}]}

            def metrics(self):
                return {"runtime_state": "READY"}

        service = OCRService(("japan",), use_subprocess=False)
        service.worker = FlakyWorker()
        result = service.analyze_page(
            Path("page.png"),
            preferred_language="japan",
            quality="Balanced",
            auto_language_fallback=False,
        )
        self.assertEqual(2, service.worker.calls)
        self.assertEqual("analyze_page", service.worker.command)
        self.assertEqual("待て", result.final_regions[0]["text"])

    def test_worker_memory_limit_waits_for_recycle_page_threshold(self):
        class AliveProcess:
            pid = 12345
            exitcode = None

            def is_alive(self):
                return True

        class ResponseConnection:
            def __init__(self):
                self.sent = []

            def poll(self, timeout):
                return True

            def recv(self):
                return {"ok": True, "state": "READY", "rss_mb": 300.0, "ocr_result": {"metadata": {}}}

            def send(self, payload):
                self.sent.append(payload)

        client = OCRWorkerClient(memory_limit_mb=100, recycle_pages=3)
        client._process = AliveProcess()
        client._connection = ResponseConnection()
        client._set_state("READY")
        with patch.object(client, "_restart") as restart:
            client.analyze_page({"image_path": "one.png"})
            restart.assert_not_called()
            client.analyze_page({"image_path": "two.png"})
            restart.assert_not_called()
            client.analyze_page({"image_path": "three.png"})
            restart.assert_called_once()
        self.assertEqual(3, client.pages_processed)

    def test_pipeline_failure_progress_ignores_requested_cancel(self):
        class FailingWorker(QObject):
            stage = Signal(str, str, int, int, str)
            image_finished = Signal(str, object)
            image_failed = Signal(str, str)
            finished = Signal(bool)

            def __init__(self, *args, **kwargs):
                super().__init__()

            def run(self):
                self.image_failed.emit("img-1", "OCR failed")

        service = PipelineService()
        token = CancellationToken()
        token.cancel()
        request = TranslationRequest(
            type=TranslationRequestType.BATCH,
            project_id="project",
            image_id="img-1",
            image_index=0,
            source_path=Path("page.png"),
            target_language="en",
            request_id="batch:img-1",
        )

        def cancelled_progress(request_id, status, message):
            self.assertEqual(TranslationRequestStatus.FAILED, status)
            raise RequestCancelled("Request cancelled")

        with patch("hydra_manga_tl.phase.pipeline.PipelineWorker", FailingWorker):
            result = service._run_request_group(
                (request,),
                token,
                cancelled_progress,
                items=[{"id": "img-1"}],
                artifacts=Path("."),
                target="en",
                config={},
            )
        self.assertEqual({"batch:img-1": None}, result)

    def test_real_worker_process_starts_and_responds(self):
        client = OCRWorkerClient()
        try:
            self.assertTrue(client.ping(timeout=15.0))
            self.assertTrue(client.alive)
        finally:
            client.close()

    def test_retry_learning_persists_aggregates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "retry_stats.json"
            store = OCRRetryStatsStore(path)
            store.record({"attempts": [
                {"reason": "low_confidence", "accepted": True, "score_delta": 0.2, "runtime_seconds": 1.5},
                {"reason": "low_confidence", "accepted": False, "score_delta": -0.1, "runtime_seconds": 0.5},
            ]})
            stats = json.loads(path.read_text(encoding="utf-8"))["reasons"]["low_confidence"]
        self.assertEqual(stats["attempts"], 2)
        self.assertEqual(stats["accepted"], 1)
        self.assertEqual(stats["rejected"], 1)
        self.assertEqual(stats["average_runtime_seconds"], 1.0)


if __name__ == "__main__":
    unittest.main()
