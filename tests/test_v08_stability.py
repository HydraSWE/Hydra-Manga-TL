from __future__ import annotations

import json
import os
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

from PIL import Image
from PySide6.QtCore import QObject, Signal

from hydra_manga_tl.export_system import export_archive, export_images
from hydra_manga_tl.job_manifest import JobManifest
from hydra_manga_tl.ocr import OCRResult, PaddleOCREngine, TextRegion
from hydra_manga_tl.ocr_manager import SmartOCRManager
from hydra_manga_tl.ocr_runtime import OCRWorkerClient, OCRWorkerCrashed
from hydra_manga_tl.ocr_service import OCRRetryStatsStore, OCRService
from hydra_manga_tl.pipeline import PipelineService
from hydra_manga_tl.project import ImageRecord, MangaProject
from hydra_manga_tl.settings import AppSettings
from hydra_manga_tl.translation_engines.base import PageDialogue, PageTranslation
from hydra_manga_tl.translation_engines.model_manager import TranslationEngineManager
from hydra_manga_tl.translation_engines.registry import EngineRegistration, TRANSLATION_PROVIDER_REGISTRY
from hydra_manga_tl.translation_engines.translation_memory import TranslationMemory
from hydra_manga_tl.translation_queue import CancellationToken, RequestCancelled
from hydra_manga_tl.translation_requests import TranslationRequest, TranslationRequestStatus, TranslationRequestType
from hydra_manga_tl.workspace import WorkspaceManager


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


class V08StabilityTests(unittest.TestCase):
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
        from hydra_manga_tl import translation_runtime

        before = len(translation_runtime._WARMUP_THREADS)
        with patch.object(translation_runtime, "_warm_marian") as warm:
            translation_runtime.start_translation_warmup(translation_engine="groq")
        self.assertEqual(len(translation_runtime._WARMUP_THREADS), before)
        warm.assert_not_called()

    def test_entrypoint_freeze_support_runs_before_app_import(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertLess(
            source.index("multiprocessing.freeze_support()"),
            source.index("from hydra_manga_tl.application import MangaApplication"),
        )

    def test_subprocess_mode_starts_global_ocr_runtime(self):
        from hydra_manga_tl import application

        previous = application.SETTINGS.ocr_subprocess_enabled
        previous_recycle = application.SETTINGS.ocr_worker_recycle_pages
        previous_memory = application.SETTINGS.ocr_worker_memory_limit_mb
        application.SETTINGS.ocr_subprocess_enabled = True
        application.SETTINGS.ocr_worker_recycle_pages = 17
        application.SETTINGS.ocr_worker_memory_limit_mb = 3072
        try:
            instance = application.MangaApplication.__new__(application.MangaApplication)
            with patch.object(application.PATHS, "initialize"), \
                    patch.object(application.logging, "basicConfig"), \
                    patch.object(application.logging, "FileHandler", return_value=Mock()), \
                    patch.object(application, "start_ocr_warmup") as ocr_warmup, \
                    patch.object(application, "start_ocr_runtime") as ocr_runtime, \
                    patch.object(application, "start_translation_warmup"):
                instance.initialize()
            ocr_warmup.assert_not_called()
            ocr_runtime.assert_called_once_with(memory_limit_mb=3072, recycle_pages=17)
        finally:
            application.SETTINGS.ocr_subprocess_enabled = previous
            application.SETTINGS.ocr_worker_recycle_pages = previous_recycle
            application.SETTINGS.ocr_worker_memory_limit_mb = previous_memory

    def test_frozen_asset_roots_include_internal_folder(self):
        from hydra_manga_tl import application

        with patch.object(application.sys, "executable", r"C:\App\Hydra Manga TL.exe"), \
                patch.object(application.sys, "_MEIPASS", r"C:\App\_internal", create=True):
            roots = application.MangaApplication._asset_roots()
        self.assertIn(Path(r"C:\App"), roots)
        self.assertIn(Path(r"C:\App\_internal"), roots)

    def test_pyinstaller_bundles_qt_text_to_speech_plugins(self):
        source = (Path(__file__).resolve().parents[1] / "HydraMangaTL.spec").read_text(encoding="utf-8")
        self.assertIn('"texttospeech"', source)

    def test_pyinstaller_embeds_application_ico_resource(self):
        source = (Path(__file__).resolve().parents[1] / "HydraMangaTL.spec").read_text(encoding="utf-8")
        self.assertIn('icon=str(PROJECT_ROOT / "assets" / "icons" / "app.ico")', source)

    def test_runtime_icon_resolver_prefers_ico(self):
        from hydra_manga_tl import application

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
            with patch("hydra_manga_tl.ocr_service.get_ocr_engine") as get_engine:
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

        with patch("hydra_manga_tl.pipeline.PipelineWorker", FailingWorker):
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
