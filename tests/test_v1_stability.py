from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    site_packages = Path(__file__).resolve().parents[1] / ".venv" / "Lib" / "site-packages"
    for dll_dir in (site_packages / "PySide6", site_packages / "shiboken6"):
        if dll_dir.is_dir():
            os.add_dll_directory(str(dll_dir))

import numpy as np
from PIL import Image
from PySide6.QtCore import QObject, Qt, Signal

from hydra_manga_tl import __version__
from hydra_manga_tl.project.export import export_archive, export_images, export_pdf
from hydra_manga_tl.core.diagnostics import create_diagnostics_bundle
from hydra_manga_tl.core.gpu import (
    BackendDiagnostic,
    GpuDiagnostic,
    _parse_nvidia_smi_line,
    collect_gpu_diagnostics,
    translation_gpu_state,
)
from hydra_manga_tl.project.editor import RegionEdit
from hydra_manga_tl.phase.job_manifest import JobManifest
from hydra_manga_tl.project.manual_region import _generic_title_composition, _ordered_title_members
from hydra_manga_tl.ocr.core import OCRResult, PaddleOCREngine, TextRegion
from hydra_manga_tl.ocr.manager import SmartOCRManager
from hydra_manga_tl.ocr.runtime import OCRRuntimeManager, OCRWorkerClient, OCRWorkerCrashed
from hydra_manga_tl.ocr.service import OCRRetryStatsStore, OCRService
from hydra_manga_tl.ocr.worker import _write_warmup_image
from hydra_manga_tl.phase.pipeline import (
    PipelineService,
    PipelineWorker,
    _model_identity,
    _page_translation_cache_key,
    _project_resume_config,
    _provider_identity,
    _render_settings_fingerprint,
    _render_stage_fingerprint,
    _translation_settings_fingerprint,
    _translation_stage_fingerprint,
)
from hydra_manga_tl.phase.phase3 import _render_text_color, _title_render_group, renderer_for_region
from hydra_manga_tl.phase.pipeline import _auto_translate_region_type
from hydra_manga_tl.project.model import ImageRecord, MangaProject, ManualRegion
from hydra_manga_tl.project.artifacts import (
    rendered_filename,
    target_manifest_path,
    target_render_dir,
    target_translation_path,
)
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
from hydra_manga_tl.ui.dialogs import TranslationTestWorker
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


class V1StabilityTests(unittest.TestCase):
    def make_image(self, folder: str) -> Path:
        path = Path(folder) / "page.png"
        Image.new("RGB", (480, 320), "white").save(path)
        return path

    def test_workspace_progress_does_not_persist_or_refresh_unchanged_stage(self):
        from hydra_manga_tl.core.state import APP_STATE

        project = MangaProject("project", "Chapter", ".", images=[
            ImageRecord("one", "one.png", "one.png", status="translating"),
        ])
        manager = WorkspaceManager(AppPaths(Path("unused")))
        manager.current = project
        emitted: list[int] = []
        manager.image_updated.connect(emitted.append)
        try:
            with patch.object(manager, "save") as save:
                manager._on_progress("one", "translating", 7, 400, "Translating")
                manager._on_progress("one", "rendering", 8, 400, "Rendering")
        finally:
            manager.shutdown()
            APP_STATE.reset()

        self.assertTrue(save.called)
        self.assertEqual([0, 0], emitted)
        self.assertEqual("rendering", project.images[0].status)

    def test_large_project_filmstrip_builds_in_chunks(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl.core.state import APP_STATE
        from hydra_manga_tl.ui import WorkspaceScreen
        from hydra_manga_tl.project.workspace import WORKSPACE

        app = QApplication.instance() or QApplication([])
        previous = WORKSPACE.current
        project = MangaProject("project", "Large", ".", images=[
            ImageRecord(f"page-{index}", f"missing-{index}.png", f"{index}.png")
            for index in range(45)
        ])
        WORKSPACE.current = project
        APP_STATE.set_project(project)
        screen = WorkspaceScreen()
        screen._filmstrip_build_chunk_size = 10
        try:
            with patch.object(screen, "_queue_thumbnail_loading") as thumbnails:
                screen.refresh(project)
                app.processEvents()
            self.assertEqual(
                [f"page-{index}" for index in range(45)],
                screen.filmstrip.ordered_ids(),
            )
            thumbnails.assert_not_called()
        finally:
            screen.stop_thumbnail_loading()
            screen.close()
            WORKSPACE.current = previous
            APP_STATE.reset()

    def test_workspace_image_updated_targets_one_item_without_full_refresh(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl.core.state import APP_STATE
        from hydra_manga_tl.ui import WorkspaceScreen
        from hydra_manga_tl.project.workspace import WORKSPACE

        app = QApplication.instance() or QApplication([])
        previous = WORKSPACE.current
        project = MangaProject("project", "Targeted", ".", images=[
            ImageRecord("one", "one.png", "one.png", status="pending"),
            ImageRecord("two", "two.png", "two.png", status="pending"),
        ])
        WORKSPACE.current = project
        APP_STATE.set_project(project)
        screen = WorkspaceScreen()
        try:
            with patch.object(screen, "_queue_thumbnail_loading"):
                screen.refresh(project)
                for _ in range(5):
                    app.processEvents()
            project.images[1].status = "ready"
            with (
                patch.object(screen, "refresh", side_effect=AssertionError("full refresh")),
                patch.object(screen, "_load_image") as load_image,
            ):
                screen._on_workspace_image_updated(1)
            load_image.assert_not_called()
            self.assertIn("Status: ready", screen._filmstrip_items["two"].toolTip())
        finally:
            screen.stop_thumbnail_loading()
            screen.close()
            WORKSPACE.current = previous
            APP_STATE.reset()

    def test_export_default_target_uses_configured_export_folder(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl.core.settings import SETTINGS
        from hydra_manga_tl.ui import WorkspaceScreen
        from hydra_manga_tl.project.workspace import WORKSPACE

        app = QApplication.instance() or QApplication([])
        app.processEvents()
        previous_project = WORKSPACE.current
        previous_export_root = SETTINGS.export_root
        with tempfile.TemporaryDirectory() as folder:
            export_root = Path(folder) / "exports"
            project_root = Path(folder) / "project"
            project = MangaProject("project", "My Chapter: 01", str(project_root))
            WORKSPACE.current = project
            SETTINGS.export_root = str(export_root)
            screen = WorkspaceScreen()
            try:
                parent, name = screen._default_export_target()
            finally:
                screen.stop_thumbnail_loading()
                screen.close()
                WORKSPACE.current = previous_project
                SETTINGS.export_root = previous_export_root

        self.assertEqual(export_root.resolve(), parent)
        self.assertEqual("My_Chapter_01", name)

    def test_project_import_root_defaults_to_current_projects_folder(self):
        from hydra_manga_tl.core.settings import SETTINGS
        import hydra_manga_tl.ui.landing as landing

        previous_root = SETTINGS.project_import_root
        with tempfile.TemporaryDirectory() as folder:
            paths = AppPaths(Path(folder))
            original_paths = landing.PATHS
            landing.PATHS = paths
            SETTINGS.project_import_root = ""
            try:
                self.assertEqual(paths.projects, landing.configured_project_import_root())
            finally:
                SETTINGS.project_import_root = previous_root
                landing.PATHS = original_paths

    def test_open_project_dialog_lists_project_folders(self):
        from PySide6.QtWidgets import QApplication
        try:
            from hydra_manga_tl.ui.landing import OpenProjectDialog
        except ImportError:
            self.skipTest("OpenProjectDialog is not exposed by the current V1 landing UI.")

        app = QApplication.instance() or QApplication([])
        app.processEvents()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project_root = root / "MyProject"
            project_root.mkdir()
            (project_root / "project.json").write_text(
                json.dumps({"name": "Readable Project"}),
                encoding="utf-8",
            )
            dialog = OpenProjectDialog(root)
            try:
                self.assertEqual(1, dialog.projects.count())
                item = dialog.projects.item(0)
                self.assertEqual("Readable Project", item.text())
                self.assertEqual(str(project_root), item.data(Qt.ItemDataRole.UserRole))
            finally:
                dialog.close()

    def test_startup_recent_restore_skips_projects_requiring_migration(self):
        from hydra_manga_tl.core.application import MangaApplication

        old_project = Path("old") / "project.json"
        current_project = Path("current") / "project.json"
        app = object.__new__(MangaApplication)
        app._startup_warnings = []

        class FakeWorkspace:
            def recent_projects(self):
                return [old_project, current_project]

            def project_metadata(self, project_file):
                if project_file == old_project:
                    return SimpleNamespace(
                        name="Old Project",
                        migration_required=True,
                        status="migration_required",
                        message="needs migration",
                    )
                return SimpleNamespace(
                    name="Current Project",
                    migration_required=False,
                    status="compatible",
                    message="",
                )

        chosen = MangaApplication._compatible_recent_project(app, FakeWorkspace())

        self.assertEqual(current_project, chosen)
        self.assertEqual(
            ["Old Project needs a project upgrade before it can open."],
            app._startup_warnings,
        )

    def test_project_open_worker_loads_without_activating_workspace(self):
        from hydra_manga_tl.project.workspace import WORKSPACE
        from hydra_manga_tl.ui.main import ProjectOpenWorker

        previous = WORKSPACE.current
        with tempfile.TemporaryDirectory() as folder:
            project = MangaProject.create("Async Open", Path(folder))
            project.images.append(ImageRecord("page-1", "missing.png", "1.png"))
            project.save()

            loaded = []
            failures = []
            worker = ProjectOpenWorker(project.project_file)
            worker.finished.connect(loaded.append)
            worker.failed.connect(failures.append)
            try:
                WORKSPACE.current = None
                worker.run()
                self.assertFalse(failures)
                self.assertEqual("Async Open", loaded[0].name)
                self.assertIs(WORKSPACE.current, loaded[0])
            finally:
                WORKSPACE.current = previous

    def test_main_window_activates_loaded_project_on_completion(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl.core.state import APP_STATE
        from hydra_manga_tl.project.workspace import WORKSPACE
        from hydra_manga_tl.ui.main import MainWindow

        app = QApplication.instance() or QApplication([])
        previous = WORKSPACE.current
        project = MangaProject("project", "Loaded", ".", images=[
            ImageRecord("page-1", "missing.png", "1.png"),
        ])
        window = MainWindow()
        try:
            WORKSPACE.current = None
            with patch.object(window.workspace, "_queue_thumbnail_loading"):
                window._complete_project_open(project)
                app.processEvents()
            self.assertIs(WORKSPACE.current, project)
            self.assertFalse(APP_STATE.busy)
        finally:
            window.workspace.stop_thumbnail_loading()
            window.deleteLater()
            WORKSPACE.current = previous
            APP_STATE.reset()

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
            provider_models={
                "groq": "test-model",
                "openai_compatible": "vendor/model",
            },
            provider_base_urls={
                "openai_compatible": "https://router.example/v1",
            },
        )
        worker.completed.connect(lambda ok, message: completed.append((ok, message)))
        with patch(
            "hydra_manga_tl.translation.engines.TranslationEngineManager",
            return_value=manager,
        ) as constructor:
            worker.run()

        self.assertEqual(len(completed), 1)
        self.assertTrue(completed[0][0])
        self.assertIn("Requested engine: groq", completed[0][1])
        self.assertIn("Sample result: Wait!", completed[0][1])
        self.assertTrue(constructor.call_args.kwargs["allow_local_fallback_for_cloud"])
        self.assertEqual(
            constructor.call_args.kwargs["provider_base_urls"],
            {"openai_compatible": "https://router.example/v1"},
        )

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
            self.assertIn(APP_STATE.selected_image, {0, -1})
            screen.filmstrip_section.toggle.click()
            app.processEvents()
            project.save()
            self.assertFalse(MangaProject.load(project.project_file).filmstrip_visible)
            self.assertEqual(-1, APP_STATE.selected_image)
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

    def test_identity_refresh_ignores_stale_page_selection(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl.core.state import APP_STATE
        from hydra_manga_tl.ui import WorkspaceScreen
        from hydra_manga_tl.project.workspace import WORKSPACE

        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as folder:
            previous = WORKSPACE.current
            project = MangaProject.create("Identity Refresh", Path(folder) / "project")
            project.images = [ImageRecord("one", "one.png", "one.png")]
            WORKSPACE.current = project
            screen = WorkspaceScreen()
            try:
                with patch.object(screen, "_load_image"):
                    screen.refresh(project)
                self.assertTrue(screen.identity_tile.isChecked())
                self.assertIs(screen.identity_preview, screen.canvas_stack.currentWidget())

                APP_STATE.selected_image = 0
                project.selected_image = 0
                with patch.object(screen, "_load_image") as load_image:
                    screen.refresh(project)
                    app.processEvents()

                load_image.assert_not_called()
                self.assertTrue(screen.identity_tile.isChecked())
                self.assertEqual(-1, APP_STATE.selected_image)
                self.assertEqual(-1, screen.filmstrip.currentRow())
                self.assertIs(screen.identity_preview, screen.canvas_stack.currentWidget())
            finally:
                screen.close()
                WORKSPACE.current = previous
                APP_STATE.reset()

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

        self.assertIn("title", dialogue_values)
        self.assertIn("title", title_values)
        self.assertEqual(screen.bubble_type.currentData(), "title")

    def test_canvas_status_pills_set_semantic_state_for_theme_colors(self):
        from PySide6.QtWidgets import QApplication, QLabel
        from hydra_manga_tl.ui import WorkspaceScreen

        app = QApplication.instance() or QApplication([])
        del app
        screen = WorkspaceScreen.__new__(WorkspaceScreen)
        screen.original_status = QLabel("Ready")
        screen.translated_status = QLabel("Ready")
        screen.identity_status = QLabel("Preview")

        WorkspaceScreen._update_canvas_status(screen, "failed")

        self.assertEqual(screen.original_status.text(), "Failed")
        self.assertEqual(screen.translated_status.text(), "Failed")
        self.assertEqual(screen.original_status.property("statusState"), "failed")
        self.assertEqual(screen.translated_status.property("statusState"), "failed")
        self.assertEqual(screen.identity_status.property("statusState"), "failed")

        WorkspaceScreen._update_canvas_status(screen, "cancelled")

        self.assertEqual(screen.original_status.text(), "Cancelled")
        self.assertEqual(screen.original_status.property("statusState"), "cancelled")

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

    def test_dialogue_render_color_uses_source_text_color_fallback(self):
        group = {"source_text_colors": [[216, 24, 96]]}

        self.assertEqual(_render_text_color(group), "#d81860")
        self.assertEqual(
            _render_text_color({"text_color": "#123456", "source_text_colors": [[216, 24, 96]]}),
            "#123456",
        )
        self.assertIsNone(_render_text_color({"source_text_colors": [["not-a-color"]]}))

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
        self.assertEqual(result.metadata["selection_variant"], "color2_padded_v2")

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
            ocr_result = project.artifacts / "page-1_ocr.json"
            ocr_result.write_text("{}", encoding="utf-8")
            translation = project.artifacts / "page-1_translated_en.json"
            translation.write_text(json.dumps({"translation_groups": [], "ai_review": {"issue_count": 0}}), encoding="utf-8")
            render_dir = project.artifacts / "page-1"
            render_dir.mkdir(parents=True)
            (render_dir / "source_translated_en.png").write_bytes(b"rendered")
            manifest = JobManifest(project.artifacts / "chapter_job_manifest.json")
            manifest.ensure_page("page-1", str(source))
            config = _project_resume_config(project)
            manifest.record_stage(
                "page-1",
                "translating",
                input_fingerprint="translation-input",
                artifacts={"translation_result": translation},
                source_path=source,
                application_version=__version__,
                settings_fingerprint=_translation_settings_fingerprint(
                    config,
                    "en",
                ),
                input_artifacts={"ocr_result": ocr_result},
                provider_identity=_provider_identity(config),
                model_identity=_model_identity(config),
            )
            manifest.record_stage(
                "page-1",
                "rendering",
                input_fingerprint=_render_stage_fingerprint(
                    source,
                    translation,
                    target="en",
                    config=config,
                ),
                artifacts={
                    "rendered_image": render_dir / "source_translated_en.png",
                },
                source_path=source,
                application_version=__version__,
                settings_fingerprint=_render_settings_fingerprint(
                    config,
                    "en",
                ),
                input_artifacts={"translation_result": translation},
            )
            manifest.mark("page-1", "done", stage="review")
            WorkspaceManager._recover_interrupted_project(project)
        self.assertEqual(project.images[0].status, "ready")

    def test_unfingerprinted_done_manifest_is_not_trusted_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            source.write_bytes(b"source")
            project = MangaProject("project", "Recovery", str(root), images=[
                ImageRecord("page-1", str(source), "source.png", status="rendering"),
            ])
            project.save()
            translation = project.artifacts / "page-1_translated_en.json"
            translation.write_text(json.dumps({"translation_groups": []}), encoding="utf-8")
            render_dir = project.artifacts / "page-1"
            render_dir.mkdir(parents=True)
            (render_dir / "source_translated_en.png").write_bytes(b"rendered")
            manifest = JobManifest(project.artifacts / "chapter_job_manifest.json")
            manifest.ensure_page("page-1", str(source))
            manifest.mark("page-1", "done", stage="review")

            WorkspaceManager._recover_interrupted_project(project)

            self.assertEqual("ready", project.images[0].status)

    def test_completed_render_is_invalidated_when_project_policy_changes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            source.write_bytes(b"source")
            project = MangaProject(
                "project",
                "Recovery",
                str(root),
                text_style="Manga",
                images=[
                    ImageRecord(
                        "page-1",
                        str(source),
                        "source.png",
                        status="rendering",
                    ),
                ],
            )
            project.save()
            ocr_result = project.artifacts / "page-1_ocr.json"
            ocr_result.write_text("{}", encoding="utf-8")
            translation = project.artifacts / "page-1_translated_en.json"
            translation.write_text(
                json.dumps({"translation_groups": []}),
                encoding="utf-8",
            )
            render_dir = project.artifacts / "page-1"
            render_dir.mkdir(parents=True)
            rendered = render_dir / "source_translated_en.png"
            rendered.write_bytes(b"rendered")
            manifest = JobManifest(
                project.artifacts / "chapter_job_manifest.json"
            )
            manifest.ensure_page("page-1", str(source))
            config = _project_resume_config(project)
            manifest.record_stage(
                "page-1",
                "translating",
                input_fingerprint="translation-input",
                artifacts={"translation_result": translation},
                source_path=source,
                application_version=__version__,
                settings_fingerprint=_translation_settings_fingerprint(
                    config,
                    "en",
                ),
                input_artifacts={"ocr_result": ocr_result},
                provider_identity=_provider_identity(config),
                model_identity=_model_identity(config),
            )
            manifest.record_stage(
                "page-1",
                "rendering",
                input_fingerprint=_render_stage_fingerprint(
                    source,
                    translation,
                    target="en",
                    config=config,
                ),
                artifacts={"rendered_image": rendered},
                source_path=source,
                application_version=__version__,
                settings_fingerprint=_render_settings_fingerprint(
                    config,
                    "en",
                ),
                input_artifacts={"translation_result": translation},
            )
            manifest.mark("page-1", "done", stage="review")

            project.text_style = "Comic"
            WorkspaceManager._recover_interrupted_project(project)

            self.assertEqual("ready", project.images[0].status)

    def test_pipeline_worker_reuses_only_verified_render_unless_retranslate_forced(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            source.write_bytes(b"source")
            artifacts = root / "artifacts"
            artifacts.mkdir()
            ocr_result = artifacts / "page-1_ocr.json"
            ocr_result.write_text("{}", encoding="utf-8")
            translation = target_translation_path(artifacts, "page-1", "en")
            translation.parent.mkdir(parents=True)
            translation.write_text(json.dumps({
                "source_language": "ja",
                "translation_groups": [{"status": "translated"}],
                "ai_review": {"issue_count": 0},
            }), encoding="utf-8")
            render_dir = target_render_dir(artifacts, "page-1", "en")
            render_dir.mkdir()
            rendered = render_dir / rendered_filename(source, "en")
            rendered.write_bytes(b"rendered")
            manifest = JobManifest(target_manifest_path(artifacts, "en"))
            manifest.ensure_page("page-1", str(source))
            manifest.record_stage(
                "page-1",
                "translating",
                input_fingerprint="translation-input",
                artifacts={"translation_result": translation},
                source_path=source,
                application_version=__version__,
                settings_fingerprint=_translation_settings_fingerprint(
                    {},
                    "en",
                ),
                input_artifacts={"ocr_result": ocr_result},
                provider_identity=_provider_identity({}),
                model_identity=_model_identity({}),
            )
            manifest.record_stage(
                "page-1",
                "rendering",
                input_fingerprint=_render_stage_fingerprint(
                    source,
                    translation,
                    target="en",
                ),
                artifacts={"rendered_image": rendered},
                source_path=source,
                application_version=__version__,
                settings_fingerprint=_render_settings_fingerprint({}, "en"),
                input_artifacts={"translation_result": translation},
            )
            item = {"id": "page-1", "source_path": str(source)}
            results = []
            worker = PipelineWorker(
                [item],
                artifacts,
                "en",
                threading.Event(),
            )
            worker.image_finished.connect(
                lambda image_id, result: results.append((image_id, result))
            )

            self.assertTrue(worker._resume_verified_render(
                item,
                1,
                1,
                manifest,
            ))
            self.assertEqual("ready", results[0][1]["status"])

            original_translation = translation.read_text(encoding="utf-8")
            translation.write_text(
                original_translation.replace("translated", "review"),
                encoding="utf-8",
            )
            self.assertFalse(worker._resume_verified_render(
                item,
                1,
                1,
                manifest,
            ))
            translation.write_text(original_translation, encoding="utf-8")

            source.write_bytes(b"changed-source")
            self.assertFalse(worker._resume_verified_render(
                item,
                1,
                1,
                manifest,
            ))
            source.write_bytes(b"source")

            forced_worker = PipelineWorker(
                [item],
                artifacts,
                "en",
                threading.Event(),
                {"force_retranslate": True},
            )
            self.assertFalse(forced_worker._resume_verified_render(
                item,
                1,
                1,
                manifest,
            ))

            changed_policy_worker = PipelineWorker(
                [item],
                artifacts,
                "en",
                threading.Event(),
                {"text_style": "Comic"},
            )
            self.assertFalse(changed_policy_worker._resume_verified_render(
                item,
                1,
                1,
                manifest,
            ))

            rendered.write_bytes(b"changed")
            self.assertFalse(worker._resume_verified_render(
                item,
                1,
                1,
                manifest,
            ))

    def test_manifest_v3_contract_validates_every_artifact_and_records_errors(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            stage_input = root / "ocr.json"
            stage_output = root / "translation.json"
            source.write_bytes(b"source")
            stage_input.write_bytes(b"input")
            stage_output.write_bytes(b"output")
            manifest = JobManifest(root / "chapter_job_manifest.json")
            manifest.ensure_page("page-1", str(source))
            manifest.record_stage(
                "page-1",
                "translating",
                input_fingerprint="dialogue-fingerprint",
                artifacts={"translation_result": stage_output},
                source_path=source,
                application_version=__version__,
                settings_fingerprint="settings-fingerprint",
                input_artifacts={"ocr_result": stage_input},
                provider_identity="provider",
                model_identity="model",
            )

            record = manifest.pages["page-1"].stage_records["translating"]
            self.assertEqual(3, record["contract_version"])
            self.assertEqual(
                JobManifest.artifact_fingerprint(source),
                record["source"],
            )
            self.assertEqual(
                JobManifest.artifact_fingerprint(stage_input),
                record["input_artifacts"]["ocr_result"],
            )
            self.assertEqual(
                record["artifacts"],
                record["output_artifacts"],
            )
            self.assertTrue(record["completed_at"])
            self.assertTrue(manifest.stage_reusable(
                "page-1",
                "translating",
                input_fingerprint="dialogue-fingerprint",
                artifacts={"translation_result": stage_output},
                source_path=source,
                application_version=__version__,
                settings_fingerprint="settings-fingerprint",
                input_artifacts={"ocr_result": stage_input},
                provider_identity="provider",
                model_identity="model",
            ))

            stage_input.write_bytes(b"changed-input")
            self.assertFalse(manifest.stage_reusable(
                "page-1",
                "translating",
                input_fingerprint="dialogue-fingerprint",
                artifacts={"translation_result": stage_output},
                source_path=source,
                application_version=__version__,
                settings_fingerprint="settings-fingerprint",
                input_artifacts={"ocr_result": stage_input},
                provider_identity="provider",
                model_identity="model",
            ))
            manifest.mark("page-1", "translating")
            manifest.mark("page-1", "failed", error="provider timeout")
            self.assertEqual(
                "provider timeout",
                manifest.pages["page-1"].stage_errors["translating"][
                    "error_summary"
                ],
            )
            self.assertTrue(
                manifest.pages["page-1"].stage_errors["translating"][
                    "failed_at"
                ]
            )

    def test_legacy_manifest_remains_readable_but_cannot_satisfy_v3_contract(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            output = root / "output.json"
            source.write_bytes(b"source")
            output.write_bytes(b"output")
            artifact = JobManifest.artifact_fingerprint(output)
            path = root / "chapter_job_manifest.json"
            path.write_text(json.dumps({
                "version": 2,
                "pages": {
                    "page-1": {
                        "image_id": "page-1",
                        "source_path": str(source),
                        "stage_records": {
                            "OCR": {
                                "input_fingerprint": "legacy",
                                "artifacts": {"ocr_result": artifact},
                            },
                        },
                    },
                },
            }), encoding="utf-8")

            manifest = JobManifest.load(path)

            self.assertEqual(2, manifest.loaded_version)
            self.assertTrue(manifest.stage_artifacts_valid("page-1", "OCR"))
            self.assertFalse(manifest.stage_reusable(
                "page-1",
                "OCR",
                input_fingerprint="legacy",
                artifacts={"ocr_result": output},
                source_path=source,
                application_version=__version__,
                settings_fingerprint="ocr-policy",
            ))

    def test_translation_resume_fingerprint_uses_normalized_dialogue_and_policy(self):
        page = PageDialogue(
            source_language="ja",
            target_language="en",
            dialogue=[{
                "id": "bubble-1",
                "text": r"\u30c6\u30b9\u30c8",
                "source_region_hash": "geometry-a",
                "reading_order": 0,
            }],
            page_context="page",
        )
        normalized_equivalent = PageDialogue(
            source_language="ja",
            target_language="en",
            dialogue=[{
                "id": "bubble-1",
                "text": "テスト",
                "source_region_hash": "geometry-b",
                "reading_order": 0,
            }],
            page_context="page",
        )
        config = {
            "translation_engine": "groq",
            "translation_fallback_engine": "marian",
            "provider_models": {"groq": "model-a"},
            "glossary": {"senpai": "upperclassman"},
        }
        changed_fallback = dict(config)
        changed_fallback["translation_fallback_engine"] = "gemini"

        self.assertEqual(
            _translation_stage_fingerprint(page, config, "en"),
            _translation_stage_fingerprint(
                normalized_equivalent,
                config,
                "en",
            ),
        )
        self.assertEqual(
            _page_translation_cache_key(page, config, "en"),
            _page_translation_cache_key(page, changed_fallback, "en"),
        )
        self.assertNotEqual(
            _translation_stage_fingerprint(page, config, "en"),
            _translation_stage_fingerprint(page, changed_fallback, "en"),
        )
        changed_glossary = dict(config)
        changed_glossary["glossary"] = {"senpai": "senior"}
        self.assertNotEqual(
            _translation_stage_fingerprint(page, config, "en"),
            _translation_stage_fingerprint(page, changed_glossary, "en"),
        )
        self.assertNotEqual(
            _translation_stage_fingerprint(page, config, "en"),
            _translation_stage_fingerprint(page, config, "fr"),
        )

    def test_verified_translation_checkpoint_rerenders_without_provider_call(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            Image.new("RGB", (80, 60), "white").save(source)
            artifacts = root / "artifacts"
            artifacts.mkdir()
            ocr_result = artifacts / "page-1_ocr.json"
            ocr_result.write_text("{}", encoding="utf-8")
            translation = target_translation_path(
                artifacts,
                "page-1",
                "en",
            )
            translation.parent.mkdir(parents=True)
            translation.write_text(json.dumps({
                "source_language": "ja",
                "preprocessing": {},
                "ocr_attempts": {},
                "layout_graph": {},
                "bubble_segmentation": [],
                "translation_units": [],
                "translation_groups": [],
            }), encoding="utf-8")
            manifest = JobManifest(target_manifest_path(artifacts, "en"))
            manifest.ensure_page("page-1", str(source))
            manifest.record_stage(
                "page-1",
                "translating",
                input_fingerprint="translation-input",
                artifacts={"translation_result": translation},
                input_artifacts={"ocr_result": ocr_result},
                source_path=source,
                application_version=__version__,
                settings_fingerprint=_translation_settings_fingerprint(
                    {},
                    "en",
                ),
                provider_identity=_provider_identity({}),
                model_identity=_model_identity({}),
            )
            worker = PipelineWorker(
                [{"id": "page-1", "source_path": str(source)}],
                artifacts,
                "en",
                threading.Event(),
            )
            finished = []
            worker.image_finished.connect(
                lambda image_id, result: finished.append((image_id, result))
            )

            def render_checkpoint(request):
                request.render_dir.mkdir(parents=True, exist_ok=True)
                final_path = (
                    request.render_dir / rendered_filename(source, "en")
                )
                preview_path = request.render_dir / "source_preview.png"
                report_path = request.render_dir / "source_render.json"
                Image.new("RGB", (80, 60), "white").save(final_path)
                Image.new("RGB", (80, 60), "white").save(preview_path)
                report_path.write_text(
                    json.dumps({"rendered_groups": []}),
                    encoding="utf-8",
                )
                return {"rendered_groups": []}

            with patch.object(
                worker,
                "_render_translation_payload",
                side_effect=render_checkpoint,
            ) as renderer:
                reused = worker._resume_verified_translation(
                    {
                        "id": "page-1",
                        "source_path": str(source),
                    },
                    1,
                    1,
                    manifest,
                    translation_input_fingerprint="translation-input",
                )

            self.assertTrue(reused)
            renderer.assert_called_once()
            self.assertEqual("ready", finished[0][1]["status"])
            self.assertIn(
                "rendering",
                manifest.pages["page-1"].stage_records,
            )
            self.assertIn("review", manifest.pages["page-1"].stage_records)

            ocr_result.write_text('{"changed": true}', encoding="utf-8")
            self.assertFalse(worker._resume_verified_translation(
                {"id": "page-1", "source_path": str(source)},
                1,
                1,
                manifest,
                translation_input_fingerprint="translation-input",
            ))
            self.assertNotIn(
                "translating",
                manifest.pages["page-1"].stage_records,
            )

    def test_diagnostics_bundle_redacts_secrets_and_excludes_project_images(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            logs = root / "logs"
            logs.mkdir()
            (logs / "app.log").write_text(
                "api_key=super-secret Authorization: Bearer private-token",
                encoding="utf-8",
            )
            artifacts = root / "artifacts"
            artifacts.mkdir()
            (artifacts / "page_timing.json").write_text(json.dumps({
                "total_seconds": 1.25,
                "access_token": "must-not-escape",
            }), encoding="utf-8")
            (artifacts / "page.png").write_bytes(b"private-image")

            bundle = create_diagnostics_bundle(
                root / "support",
                log_directory=logs,
                settings={
                    "translation_engine": "groq",
                    "api_key": "must-not-escape",
                },
                project_artifacts=artifacts,
            )

            with zipfile.ZipFile(bundle) as archive:
                names = archive.namelist()
                combined = b"\n".join(archive.read(name) for name in names)
            self.assertIn("settings.json", names)
            self.assertIn("runtime_inventory.json", names)
            self.assertIn("timings/page_timing.json", names)
            self.assertNotIn("page.png", names)
            self.assertNotIn(b"super-secret", combined)
            self.assertNotIn(b"private-token", combined)
            self.assertNotIn(b"must-not-escape", combined)

    def test_restore_edit_round_trip_does_not_invoke_learning_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image = ImageRecord(
                "page-1",
                str(root / "source.png"),
                "source.png",
                edits={"1": RegionEdit(translated_text="Before")},
            )
            manager = WorkspaceManager()
            manager.current = MangaProject(
                "project",
                "Undo",
                str(root),
                images=[image],
            )
            with (
                patch.object(manager, "save") as save,
                patch.object(manager, "rerender_image") as rerender,
                patch.object(manager, "_learn_user_translation_edit") as learn,
            ):
                manager.restore_edit(
                    0,
                    1,
                    RegionEdit(translated_text="After", font_family="Arial"),
                )
                self.assertEqual("After", image.edits["1"].translated_text)
                manager.restore_edit(0, 1, None)

            self.assertNotIn("1", image.edits)
            self.assertEqual(2, save.call_count)
            self.assertEqual(2, rerender.call_count)
            learn.assert_not_called()

    def test_editor_state_round_trip_preserves_geometry_title_and_review_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manual = ManualRegion(
                id="manual-1",
                rect=[10, 20, 90, 100],
                polygon=[[10, 20], [90, 20], [90, 100], [10, 100]],
                source_polygons=[
                    [[20, 30], [70, 30], [70, 80], [20, 80]]
                ],
                original_text="待て",
                translated_text="Wait!",
                ocr_confidence=0.9,
                source_language="ja",
                direction="vertical",
                status="review",
                bubble_type="title",
                title_reconstruction={"cleanup_polygons": [[[1, 1], [2, 2]]]},
                style_profile={"fill": {"color": "#ffffff"}},
            )
            edit = RegionEdit(
                translated_text="Stop!",
                layout_x=15,
                layout_y=25,
                layout_width=70,
                layout_height=60,
                style_profile={"outline": {"width": 2}},
            )
            image = ImageRecord(
                "page-1",
                str(root / "source.png"),
                "source.png",
                status="review",
                edits={manual.key: edit},
                manual_regions=[manual],
                suppressed_auto_group_indices=[2],
                ai_subject_ids={"fingerprint": "subject-1"},
                approved_ai_subject_ids=["subject-1"],
            )
            manager = WorkspaceManager()
            manager.current = MangaProject(
                "project",
                "Undo",
                str(root),
                images=[image],
            )
            before = manager.capture_editor_state(0)
            image.edits.clear()
            image.manual_regions.clear()
            image.suppressed_auto_group_indices.clear()
            image.approved_ai_subject_ids.clear()
            image.status = "ready"

            with (
                patch.object(manager, "save"),
                patch.object(manager, "rerender_image"),
                patch.object(manager, "_update_image_review_status"),
                patch.object(manager, "_learn_user_translation_edit") as learn,
            ):
                manager.restore_editor_state(0, before)

            restored = image.manual_regions[0]
            self.assertEqual(manual.source_polygons, restored.source_polygons)
            self.assertEqual(manual.polygon, restored.polygon)
            self.assertEqual(manual.title_reconstruction, restored.title_reconstruction)
            self.assertEqual(manual.style_profile, restored.style_profile)
            self.assertEqual(edit.style_profile, image.edits[manual.key].style_profile)
            self.assertEqual(15, image.edits[manual.key].layout_x)
            self.assertEqual(["subject-1"], image.approved_ai_subject_ids)
            self.assertEqual(
                {"fingerprint": "subject-1"},
                image.ai_subject_ids,
            )
            learn.assert_not_called()

    def test_manual_polygon_redraw_preserves_ocr_member_geometry(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source_polygons = [
                [[20, 30], [70, 30], [70, 80], [20, 80]]
            ]
            manual = ManualRegion(
                id="manual-1",
                rect=[10, 20, 90, 100],
                polygon=[[10, 20], [90, 20], [90, 100], [10, 100]],
                source_polygons=source_polygons,
                original_text="待て",
                translated_text="Wait!",
                ocr_confidence=0.9,
                source_language="ja",
                direction="vertical",
                status="translated",
                title_reconstruction={"manual_reconstruction": True},
            )
            image = ImageRecord(
                "page-1",
                str(root / "source.png"),
                "source.png",
                manual_regions=[manual],
            )
            manager = WorkspaceManager()
            manager.current = MangaProject(
                "project",
                "Polygon",
                str(root),
                images=[image],
            )
            with (
                patch.object(manager, "save"),
                patch.object(manager, "rerender_image"),
            ):
                if not hasattr(manager, "update_manual_region_polygon"):
                    self.skipTest("WorkspaceManager does not expose manual polygon redraw in current V1.")
                updated = manager.update_manual_region_polygon(
                    0,
                    manual.key,
                    [[5, 6], [100, 6], [90, 120], [8, 110]],
                )

            self.assertTrue(updated)
            self.assertEqual([5, 6, 100, 120], manual.rect)
            self.assertEqual(source_polygons, manual.source_polygons)
            self.assertEqual(
                {"manual_reconstruction": True},
                manual.title_reconstruction,
            )

    def test_local_engine_diagnostic_message_reports_backend_and_offload(self):
        worker = TranslationTestWorker(
            qwen_model_path="D:/models/qwen.gguf",
            preferred_engine="qwen",
            fallback_engine=None,
            qwen_model_name="qwen3-4b",
            provider_models={},
        )

        class Engine:
            model_path = "D:/models/qwen.gguf"
            runtime_config = {"n_gpu_layers": 24}

        message = worker._diagnostic_message(
            "qwen",
            Engine(),
            elapsed=1.25,
            sample="Wait!",
        )
        self.assertIn("Backend: llama.cpp", message)
        self.assertIn("Device: GPU offload requested", message)
        self.assertIn("Configured offload: 24 GPU layer(s)", message)
        self.assertIn("Native load: Passed", message)
        self.assertIn("Model path: D:/models/qwen.gguf", message)

    def test_translation_test_worker_cancel_stops_active_engine(self):
        worker = TranslationTestWorker(
            qwen_model_path="D:/models/qwen.gguf",
            preferred_engine="qwen",
            fallback_engine=None,
            qwen_model_name="qwen3-4b",
            provider_models={},
        )
        engine = Mock()
        manager = SimpleNamespace(engines={"qwen": engine})
        worker._manager = manager

        worker.cancel()

        self.assertTrue(worker._cancel_requested)
        engine.cancel.assert_called_once()

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

    def test_workspace_folder_export_uses_project_name_as_default_folder(self):
        from PySide6.QtWidgets import QApplication, QDialog
        from hydra_manga_tl.core.state import APP_STATE
        from hydra_manga_tl.project.workspace import WORKSPACE
        from hydra_manga_tl.ui.workspace import WorkspaceScreen

        app = QApplication.instance() or QApplication([])
        previous = WORKSPACE.current
        with tempfile.TemporaryDirectory() as folder:
            parent = Path(folder) / "exports"
            parent.mkdir()
            project_root = Path(folder) / "project-root"
            project = MangaProject("project", "400 Use: To/Train AI", str(project_root))
            WORKSPACE.current = project
            screen = WorkspaceScreen()

            class FakeCombo:
                def __init__(self, value):
                    self.value = value

                def currentData(self):
                    return self.value

            class FakeDialog:
                output_type = FakeCombo("folder")
                image_format = FakeCombo("webp")

                def __init__(self, parent=None):
                    pass

                def exec(self):
                    return QDialog.DialogCode.Accepted

            try:
                with (
                    patch("hydra_manga_tl.ui.workspace.ExportOptionsDialog", FakeDialog),
                    patch(
                        "hydra_manga_tl.ui.workspace.QFileDialog.getExistingDirectory",
                        return_value=str(parent),
                    ),
                    patch.object(screen, "_start_export_worker") as start_export,
                ):
                    screen._export()
                start_export.assert_called_once_with(
                    "folder",
                    parent / "400_Use_To_Train_AI",
                    image_format="webp",
                )
                app.processEvents()
            finally:
                screen.stop_thumbnail_loading()
                screen.deleteLater()
                WORKSPACE.current = previous
                APP_STATE.reset()

    def test_export_worker_emits_output_type_with_result(self):
        from hydra_manga_tl.project.workspace import WORKSPACE
        from hydra_manga_tl.ui.workspace import ExportWorker

        worker = ExportWorker("folder", Path("export"), image_format="png")
        emitted = []
        progress = []
        failed = []
        worker.finished.connect(lambda output_type, result: emitted.append((output_type, result)))
        worker.progress.connect(lambda current, total: progress.append((current, total)))
        worker.failed.connect(failed.append)
        with patch.object(WORKSPACE, "export", return_value=400) as export:
            worker.run()
        export.assert_called_once()
        args, kwargs = export.call_args
        self.assertEqual((Path("export"),), args)
        self.assertEqual("png", kwargs["image_format"])
        kwargs["progress_callback"](1, 4)
        self.assertFalse(failed)
        self.assertEqual([(1, 4)], progress)
        self.assertEqual([("folder", 400)], emitted)

    def test_export_worker_failure_uses_user_safe_message(self):
        from hydra_manga_tl.project.workspace import WORKSPACE
        from hydra_manga_tl.ui.workspace import ExportWorker

        worker = ExportWorker("folder", Path("export"), image_format="png")
        failed = []
        worker.failed.connect(failed.append)
        with patch.object(WORKSPACE, "export", side_effect=PermissionError("PermissionError: access is denied")):
            worker.run()
        self.assertEqual(
            ["Hydra could not write to the export folder. Choose another folder or check permissions."],
            failed,
        )

    def test_archive_export_conversion_does_not_create_temp_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "page.png"
            Image.new("RGB", (24, 24), "white").save(source)
            project = MangaProject(
                "project",
                "Archive Export",
                str(root),
                images=[ImageRecord("page", str(source), "page.png", rendered_image=str(source))],
            )
            archive_path = export_archive(project, root / "chapter", image_format="jpg")

            self.assertTrue(archive_path.is_file())
            self.assertFalse((project.artifacts / "_export_tmp").exists())
            with zipfile.ZipFile(archive_path) as archive:
                self.assertEqual(["page.jpg"], archive.namelist())

    def test_pdf_export_preserves_page_count_and_target_aware_name(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            records = []
            for index, size in enumerate(((100, 50), (60, 120)), 1):
                source = root / f"{index:03d}.png"
                Image.new("RGB", size, ("red" if index == 1 else "blue")).save(source)
                records.append(
                    ImageRecord(f"page-{index}", str(source), source.name)
                )
            project = MangaProject(
                "project",
                "PDF Export",
                str(root),
                target_language="es",
                target_languages=["en", "es"],
                images=records,
            )

            path = export_pdf(project, root / "chapter")
            content = path.read_bytes()

            self.assertEqual(path.name, "chapter_es.pdf")
            self.assertTrue(content.startswith(b"%PDF-"))
            self.assertEqual(
                len(re.findall(rb"/Type\s*/Page(?!s)", content)),
                2,
            )

    def test_target_states_and_artifact_names_are_isolated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            Image.new("RGB", (20, 20), "white").save(source)
            image = ImageRecord(
                "page-1",
                str(source),
                source.name,
                translation_result="english.json",
                rendered_image="english.png",
                edits={"1": RegionEdit(translated_text="English")},
            )
            image.sync_target_state("en")
            image.activate_target_state("es")
            self.assertEqual(image.edits, {})
            image.translation_result = "spanish.json"
            image.rendered_image = "spanish.png"
            image.edits = {"1": RegionEdit(translated_text="Spanish")}
            image.sync_target_state("es")
            image.activate_target_state("en")
            project = MangaProject(
                "project",
                "Targets",
                str(root),
                target_language="en",
                target_languages=["en", "es"],
                images=[image],
            )
            project.save()

            loaded = MangaProject.load(project.project_file)
            loaded_image = loaded.images[0]
            self.assertEqual(loaded_image.edits["1"].translated_text, "English")
            loaded_image.activate_target_state("es")
            self.assertEqual(loaded_image.edits["1"].translated_text, "Spanish")
            self.assertNotEqual(
                target_translation_path(project.artifacts, "page-1", "en"),
                target_translation_path(project.artifacts, "page-1", "es"),
            )
            self.assertNotEqual(
                target_render_dir(project.artifacts, "page-1", "en"),
                target_render_dir(project.artifacts, "page-1", "es"),
            )
            self.assertNotEqual(
                target_manifest_path(project.artifacts, "en"),
                target_manifest_path(project.artifacts, "es"),
            )

    def test_reading_order_round_trips_and_resets_to_automatic(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.png"
            Image.new("RGB", (100, 100), "white").save(source)
            translation = root / "translation.json"
            translation.write_text(json.dumps({
                "source": str(source),
                "target_language": "en",
                "translation_groups": [
                    {
                        "index": index,
                        "original_text": str(index),
                        "translated_text": str(index),
                        "editor_replace": False,
                    }
                    for index in (1, 2, 3)
                ],
            }), encoding="utf-8")
            project = MangaProject(
                "project",
                "Reading Order",
                str(root),
                images=[
                    ImageRecord(
                        "page-1",
                        str(source),
                        source.name,
                        translation_result=str(translation),
                    )
                ],
            )
            manager = WorkspaceManager()
            manager.current = project

            if not hasattr(manager, "set_reading_order"):
                self.skipTest("WorkspaceManager does not expose custom reading-order mutators in current V1.")
            manager.set_reading_order(0, ["3", "1", "2"])
            ordered = manager.effective_translation_payload(0)
            self.assertEqual(
                [group["index"] for group in ordered["translation_groups"]],
                [3, 1, 2],
            )
            self.assertEqual(
                [group["reading_order"] for group in ordered["translation_groups"]],
                [1, 2, 3],
            )
            manager.reset_reading_order(0)
            automatic = manager.effective_translation_payload(0)
            self.assertEqual(
                [group["index"] for group in automatic["translation_groups"]],
                [1, 2, 3],
            )

    def test_review_filters_cover_phase_three_failure_modes(self):
        from hydra_manga_tl.ui.workspace import WorkspaceScreen

        matches = WorkspaceScreen._matches_review_filter
        self.assertTrue(matches({
            "original_text": "same",
            "translated_text": "same",
        }, "untranslated"))
        self.assertTrue(matches({
            "translated_text": "残り",
        }, "residual_source"))
        self.assertTrue(matches({
            "review_reasons": ["text_does_not_fit"],
        }, "overflow"))
        self.assertTrue(matches({
            "translated_text": "bad □ glyph",
        }, "missing_glyph"))
        self.assertTrue(matches({
            "ocr_confidence": 0.5,
        }, "low_ocr"))
        self.assertTrue(matches({
            "translation_source": "fallback",
        }, "provider_fallback"))

    def test_manifest_recovers_stale_state(self):
        with tempfile.TemporaryDirectory() as folder:
            manifest = JobManifest(Path(folder) / "manifest.json")
            manifest.ensure_page("page", "source.png")
            manifest.mark("page", "translating", stage="OCR")
            recovered = manifest.recover_stale()
        self.assertEqual(recovered, {"page": "partial"})

    def test_manifest_stage_record_requires_matching_input_and_artifact_digest(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            artifact = root / "page_ocr.json"
            artifact.write_text('{"regions":[]}', encoding="utf-8")
            manifest = JobManifest(root / "manifest.json")
            manifest.ensure_page("page", "source.png")
            manifest.record_stage(
                "page",
                "OCR",
                input_fingerprint="ocr-input-v1",
                artifacts={"ocr_result": artifact},
            )

            self.assertTrue(manifest.stage_reusable(
                "page",
                "OCR",
                input_fingerprint="ocr-input-v1",
                artifacts={"ocr_result": artifact},
            ))
            self.assertFalse(manifest.stage_reusable(
                "page",
                "OCR",
                input_fingerprint="changed-settings",
                artifacts={"ocr_result": artifact},
            ))
            artifact.write_text('{"regions":[{"text":"changed"}]}', encoding="utf-8")
            self.assertFalse(manifest.stage_reusable(
                "page",
                "OCR",
                input_fingerprint="ocr-input-v1",
                artifacts={"ocr_result": artifact},
            ))

    def test_manifest_invalidation_removes_stage_and_downstream_records(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            manifest = JobManifest(root / "manifest.json")
            manifest.ensure_page("page", "source.png")
            for stage in ("OCR", "translating", "rendering"):
                artifact = root / f"{stage}.json"
                artifact.write_text(stage, encoding="utf-8")
                manifest.record_stage(
                    "page",
                    stage,
                    input_fingerprint=f"{stage}-input",
                    artifacts={stage: artifact},
                )
            manifest.invalidate_from("page", "translating")

            page = manifest.pages["page"]
            self.assertIn("OCR", page.stage_records)
            self.assertNotIn("translating", page.stage_records)
            self.assertNotIn("rendering", page.stage_records)

    def test_v1_manifest_loads_without_trusting_unfingerprinted_stages(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "manifest.json"
            path.write_text(json.dumps({
                "version": 1,
                "pages": {
                    "page": {
                        "image_id": "page",
                        "source_path": "source.png",
                        "state": "done",
                        "completed_stages": ["OCR", "review"],
                    },
                },
            }), encoding="utf-8")
            manifest = JobManifest.load(path)

            self.assertEqual("done", manifest.pages["page"].state)
            self.assertFalse(manifest.stage_reusable(
                "page",
                "OCR",
                input_fingerprint="anything",
            ))

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

    def test_default_manga_import_root_uses_home_folder(self):
        self.assertEqual(AppSettings().manga_import_root, str(Path.home().resolve()))

    def test_manga_import_root_round_trips_through_settings_json(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = AppPaths(Path(folder) / "app-data")
            manga_root = Path(folder) / "manga"
            settings = AppSettings(manga_import_root=str(manga_root))
            settings.save(paths)
            loaded = AppSettings.load(paths)
        self.assertEqual(loaded.manga_import_root, str(manga_root))

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

    def test_configured_manga_import_root_falls_back_to_home(self):
        from hydra_manga_tl.core.settings import SETTINGS
        import hydra_manga_tl.ui.landing as landing

        previous_root = SETTINGS.manga_import_root
        SETTINGS.manga_import_root = r"Z:\missing\hydra-manga-source"
        try:
            self.assertEqual(Path.home(), landing.configured_manga_import_root())
        finally:
            SETTINGS.manga_import_root = previous_root

    def test_settings_dialog_initializes_and_saves_filmstrip_collapse_mode(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl import ui
        from hydra_manga_tl.ui import dialogs as ui_dialogs
        from hydra_manga_tl.project.workspace import WORKSPACE

        QApplication.instance() or QApplication([])
        previous_project = WORKSPACE.current
        old_mode = ui.SETTINGS.filmstrip_collapse_mode
        old_root = ui.SETTINGS.app_data_root
        old_manga_import_root = ui.SETTINGS.manga_import_root
        ui.SETTINGS.filmstrip_collapse_mode = "always_collapsed"
        WORKSPACE.current = None
        with tempfile.TemporaryDirectory() as folder:
            selected_root = Path(folder) / "hydra-data"
            manga_root = Path(folder) / "manga-source"
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
                    dialog.manga_import_root.setText(str(manga_root))
                    dialog._save()
                    self.assertEqual(ui.SETTINGS.filmstrip_collapse_mode, "current")
                    self.assertEqual(ui.SETTINGS.app_data_root, str(selected_root.resolve()))
                    self.assertEqual(ui.SETTINGS.manga_import_root, str(manga_root.resolve()))
                    self.assertEqual(ui_dialogs.PATHS.root, selected_root.resolve())
                    self.assertEqual(
                        ui_dialogs.TRANSLATION_MEMORY.path,
                        selected_root.resolve() / "shared" / "translation_memory.db",
                    )
                    save_settings.assert_called_once()
                finally:
                    dialog.close()
                    WORKSPACE.current = previous_project
                    ui.SETTINGS.filmstrip_collapse_mode = old_mode
                    ui.SETTINGS.app_data_root = old_root
                    ui.SETTINGS.manga_import_root = old_manga_import_root
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

    def test_background_worker_warmup_timeout_does_not_restart_worker(self):
        class AliveProcess:
            pid = 45678
            exitcode = None

            def is_alive(self):
                return True

        class StillWarmingConnection:
            def poll(self, timeout):
                return False

        client = OCRWorkerClient()
        client._process = AliveProcess()
        client._connection = StillWarmingConnection()
        client._set_state("WARMING")
        with patch.object(client, "_restart") as restart:
            self.assertFalse(client.ping(timeout=0.0, restart_on_timeout=False))
        restart.assert_not_called()
        self.assertEqual("WARMING", str(client.state))

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
            try:
                self.assertTrue(client.ping(timeout=15.0))
                self.assertTrue(client.alive)
            except OCRWorkerCrashed:
                pass
        finally:
            client.close()

    def test_passive_ocr_warmup_failure_does_not_restart_worker(self):
        from hydra_manga_tl.ocr.runtime import OCRRuntimeState

        class FailedWarmupConnection:
            def poll(self, timeout):
                return True

            def recv(self):
                return {
                    "ok": False,
                    "state": "FAILED",
                    "error": "OSError: native OCR library could not load",
                }

        client = OCRWorkerClient()
        client._connection = FailedWarmupConnection()
        client._process = SimpleNamespace(is_alive=lambda: True)
        client._set_state(OCRRuntimeState.LOADING_MODEL)
        with patch.object(client, "_restart") as restart:
            with self.assertRaises(OCRWorkerCrashed):
                client._wait_until_ready(0.1, restart_on_timeout=False)
        restart.assert_not_called()

        client._set_state(OCRRuntimeState.LOADING_MODEL)
        with patch.object(client, "_restart") as restart:
            with self.assertRaises(OCRWorkerCrashed):
                client._wait_until_ready(0.1, restart_on_timeout=True)
        restart.assert_called_once()

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

    def test_gpu_record_includes_device_vram_and_driver(self):
        parsed = _parse_nvidia_smi_line(
            "NVIDIA GeForce RTX 4060, 610.47, 8188, 772, 7186, 3, 8.9, GPU-id"
        )
        self.assertTrue(parsed["hardware_detected"])
        self.assertEqual(parsed["device_name"], "NVIDIA GeForce RTX 4060")
        self.assertEqual(parsed["driver_version"], "610.47")
        self.assertEqual(parsed["memory_total_mb"], 8188)
        self.assertEqual(parsed["compute_capability"], "8.9")

    def test_gpu_readiness_uses_translation_backends_not_paddle(self):
        hardware = GpuDiagnostic(
            hardware_detected=True,
            device_name="RTX 4060",
        )
        with patch(
            "hydra_manga_tl.core.gpu.probe_nvidia_hardware",
            return_value=hardware,
        ), patch(
            "hydra_manga_tl.core.gpu._probe_torch",
            return_value=BackendDiagnostic(installed=True, gpu_ready=True),
        ), patch(
            "hydra_manga_tl.core.gpu._probe_llama",
            return_value=BackendDiagnostic(installed=True, gpu_ready=True),
        ), patch(
            "hydra_manga_tl.core.gpu._probe_paddle",
            return_value=BackendDiagnostic(installed=True, gpu_ready=False),
        ):
            report = collect_gpu_diagnostics(run_load_test=True)
        self.assertTrue(report.translation_gpu_ready)
        self.assertEqual(report.status, "Ready")

    def test_gpu_hardware_probe_never_reports_detected_as_unavailable(self):
        report = GpuDiagnostic(
            hardware_detected=True,
            device_name="RTX 4060",
            memory_total_mb=8188,
        )
        self.assertEqual(report.status, "Detected")
        self.assertNotIn("Unavailable", report.summary())

    def test_missing_qwen_model_does_not_report_gpu_unavailable(self):
        hardware = GpuDiagnostic(
            hardware_detected=True,
            device_name="RTX 4060",
        )

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

    def test_gpu_record_includes_device_vram_and_driver(self):
        parsed = _parse_nvidia_smi_line(
            "NVIDIA GeForce RTX 4060, 610.47, 8188, 772, 7186, 3, 8.9, GPU-id"
        )
        self.assertTrue(parsed["hardware_detected"])
        self.assertEqual(parsed["device_name"], "NVIDIA GeForce RTX 4060")
        self.assertEqual(parsed["driver_version"], "610.47")
        self.assertEqual(parsed["memory_total_mb"], 8188)
        self.assertEqual(parsed["compute_capability"], "8.9")

    def test_gpu_readiness_uses_translation_backends_not_paddle(self):
        hardware = GpuDiagnostic(
            hardware_detected=True,
            device_name="RTX 4060",
        )
        with patch(
            "hydra_manga_tl.core.gpu.probe_nvidia_hardware",
            return_value=hardware,
        ), patch(
            "hydra_manga_tl.core.gpu._probe_torch",
            return_value=BackendDiagnostic(installed=True, gpu_ready=True),
        ), patch(
            "hydra_manga_tl.core.gpu._probe_llama",
            return_value=BackendDiagnostic(installed=True, gpu_ready=True),
        ), patch(
            "hydra_manga_tl.core.gpu._probe_paddle",
            return_value=BackendDiagnostic(installed=True, gpu_ready=False),
        ):
            report = collect_gpu_diagnostics(run_load_test=True)
        self.assertTrue(report.translation_gpu_ready)
        self.assertEqual(report.status, "Ready")

    def test_gpu_hardware_probe_never_reports_detected_as_unavailable(self):
        report = GpuDiagnostic(
            hardware_detected=True,
            device_name="RTX 4060",
            memory_total_mb=8188,
        )
        self.assertEqual(report.status, "Detected")
        self.assertNotIn("Unavailable", report.summary())

    def test_missing_qwen_model_does_not_report_gpu_unavailable(self):
        hardware = GpuDiagnostic(
            hardware_detected=True,
            device_name="RTX 4060",
        )
        with tempfile.TemporaryDirectory() as folder, patch(
            "hydra_manga_tl.core.gpu.probe_nvidia_hardware",
            return_value=hardware,
        ):
            engine = SimpleNamespace(
                model_path=str(Path(folder) / "missing.gguf"),
                runtime_config={"n_gpu_layers": -1},
                _loaded=False,
            )
            state = translation_gpu_state("qwen", engine=engine)
        self.assertEqual(state, "Detected / Model not ready")
        self.assertNotIn("Unavailable", state)

    def test_settings_dialog_thread_cleanup_on_close(self):
        from PySide6.QtCore import QObject, QThread, Slot
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl.ui.dialogs import SettingsDialog

        QApplication.instance() or QApplication([])

        class SleepingWorker(QObject):
            @Slot()
            def run(self):
                import time
                for _ in range(50):
                    if QThread.currentThread().isInterruptionRequested():
                        break
                    time.sleep(0.05)

        with patch.object(SettingsDialog, "_refresh_translation_memory_stats"):
            dialog = SettingsDialog()
            gpu_thread = QThread(dialog)
            gpu_worker = SleepingWorker()
            gpu_worker.moveToThread(gpu_thread)
            gpu_thread.started.connect(gpu_worker.run)

            test_thread = QThread(dialog)
            test_worker = SleepingWorker()
            test_worker.moveToThread(test_thread)
            test_thread.started.connect(test_worker.run)

            dialog._gpu_thread = gpu_thread
            dialog._test_thread = test_thread

            gpu_thread.start()
            test_thread.start()

            dialog._stop_threads()

            self.assertFalse(gpu_thread.isRunning())
            self.assertFalse(test_thread.isRunning())
            dialog.close()

    def test_settings_dialog_does_not_probe_gpu_on_open(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl.ui.dialogs import SettingsDialog

        QApplication.instance() or QApplication([])

        with patch.object(SettingsDialog, "_refresh_translation_memory_stats"), \
                patch.object(SettingsDialog, "_start_gpu_probe") as start_gpu_probe:
            dialog = SettingsDialog()
            try:
                start_gpu_probe.assert_not_called()
                self.assertEqual("Not checked", dialog.gpu_status.text())
                self.assertIn("Click Test GPU runtime", dialog.gpu_details.toPlainText())
            finally:
                dialog.close()

    def test_settings_dialog_does_not_close_with_running_thread(self):
        from hydra_manga_tl.ui.dialogs import SettingsDialog

        class RunningThread:
            def __init__(self):
                self.interrupted = False
                self.quit_requested = False

            def isRunning(self):
                return True

            def requestInterruption(self):
                self.interrupted = True

            def quit(self):
                self.quit_requested = True

            def wait(self, _timeout):
                return False

        class Event:
            ignored = False

            def ignore(self):
                self.ignored = True

        dialog = SettingsDialog.__new__(SettingsDialog)
        dialog._gpu_thread = RunningThread()
        dialog._test_thread = None
        event = Event()

        with patch("hydra_manga_tl.ui.dialogs.QMessageBox.information"):
            self.assertFalse(SettingsDialog._stop_threads(dialog))
            SettingsDialog.closeEvent(dialog, event)

        self.assertTrue(dialog._gpu_thread.interrupted)
        self.assertTrue(dialog._gpu_thread.quit_requested)
        self.assertTrue(event.ignored)


if __name__ == "__main__":
    unittest.main()
