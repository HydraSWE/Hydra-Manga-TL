from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from hydra_manga_tl.core.paths import AppPaths
from hydra_manga_tl.core.settings import AppSettings
from hydra_manga_tl.phase.pipeline import PipelineWorker
from hydra_manga_tl.translation.engines import PageDialogue, PageTranslation
from hydra_manga_tl.translation.runtime import FastTranslationSession, TranslationRuntimeConfig
from hydra_manga_tl.translation.scheduler import (
    ParallelPageJob,
    ParallelPageScheduler,
    PageTranslationOutcome,
    SchedulerSnapshot,
    auto_worker_count,
    resolve_worker_count,
    timed_stage,
)


def _job(root: Path, index: int) -> ParallelPageJob:
    return ParallelPageJob(
        page_index=index,
        image_id=f"page-{index}",
        request_id=f"batch:page-{index}",
        prepared_path=root / f"{index}.json",
        cache_path=root / f"{index}.cache.json",
        page=PageDialogue("Japanese", "en", [{"id": "r1", "text": str(index)}]),
    )


class ParallelPageSchedulerTests(unittest.TestCase):
    def test_worker_tiers_and_override(self):
        self.assertEqual(auto_worker_count(4), 2)
        self.assertEqual(auto_worker_count(8), 4)
        self.assertEqual(auto_worker_count(9), 6)
        self.assertEqual(resolve_worker_count(6, 3), 3)
        self.assertEqual(resolve_worker_count(1, 20), 1)

    def test_fast_worker_override_round_trips(self):
        with TemporaryDirectory() as raw:
            paths = AppPaths(Path(raw))
            AppSettings(fast_worker_override=5).save(paths)
            self.assertEqual(AppSettings.load(paths).fast_worker_override, 5)

    def test_pipeline_routes_only_fast_mode_to_parallel_path(self):
        worker = PipelineWorker([], Path("."), "en", threading.Event(), {"quality": "Fast"})
        emitted = []
        worker.finished.connect(emitted.append)
        with patch.object(worker, "_run_fast", return_value=False) as fast:
            worker.run()
        fast.assert_called_once_with()
        self.assertEqual(emitted, [False])

    def test_fast_context_is_immutable_ocr_source_context(self):
        worker = PipelineWorker([], Path("."), "en", threading.Event(), {
            "quality": "Fast", "glossary": {"先輩": "Senpai"},
        })
        prepared = [
            {"position": 1, "dialogue": [{"id": "r1", "text": "前"}]},
            {"position": 2, "dialogue": [{"id": "r1", "text": "今"}]},
            {"position": 3, "dialogue": [{"id": "r1", "text": "後"}]},
        ]
        context = worker._fast_page_context(prepared, 1)
        self.assertIn("p1:r1=前", context)
        self.assertIn("p3:r1=後", context)
        self.assertIn("先輩=Senpai", context)
        self.assertNotIn("translated_text", context)

    def test_fast_pipeline_schedules_then_commits_pages_in_order(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            items = [
                {"id": f"p{index}", "source_path": str(root / f"{index}.png")}
                for index in range(4)
            ]
            worker = PipelineWorker(items, root, "en", threading.Event(), {
                "quality": "Fast", "translation_engine": "groq", "fast_worker_override": 4,
            })
            committed = []

            def prepare(item, position, total, ocr_service, requested, preferred, manifest):
                manifest.ensure_page(item["id"], item["source_path"])
                payload = {
                    "source": item["source_path"],
                    "source_language": "Japanese",
                    "dialogue": [{"id": "r1", "text": item["id"]}],
                }
                path = root / "fast_jobs" / f"{position - 1:06d}_{item['id']}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(__import__("json").dumps(payload), encoding="utf-8")
                return {
                    "path": str(path), "dialogue": payload["dialogue"],
                    "position": position, "image_id": item["id"],
                }

            def translate(session, job):
                time.sleep((3 - job.page_index) * 0.01)
                return PageTranslationOutcome(
                    page_index=job.page_index,
                    image_id=job.image_id,
                    request_id=job.request_id,
                    translation=PageTranslation("Japanese", "en", [{"id": "r1", "text": "ok"}]),
                    provider_id="groq",
                    attempts=1,
                )

            fake_ocr = type("FakeOCR", (), {"close": lambda self: None})()
            fake_session = type("FakeSession", (), {"gpu_state": "Cloud / Not Used"})()
            with patch("hydra_manga_tl.phase.pipeline.OCRService", return_value=fake_ocr), \
                    patch.object(worker, "_prepare_fast_page", side_effect=prepare), \
                    patch.object(worker, "_translate_fast_job", side_effect=translate), \
                    patch.object(worker, "_commit_fast_outcome", side_effect=lambda outcome, _: committed.append(outcome.page_index) or {}), \
                    patch("hydra_manga_tl.phase.pipeline.TRANSLATION_RUNTIME.fast_session", return_value=fake_session):
                cancelled = worker._run_fast()
            self.assertFalse(cancelled)
            self.assertEqual(committed, [0, 1, 2, 3])

    def test_out_of_order_translation_commits_in_order(self):
        with TemporaryDirectory() as raw:
            jobs = [_job(Path(raw), index) for index in range(4)]
            finished = []
            committed = []
            snapshots = []

            def stage(job):
                time.sleep((3 - job.page_index) * 0.015)
                finished.append(job.page_index)
                return timed_stage(job, lambda page: (
                    PageTranslation(page.source_language, page.target_language, [
                        {"id": "r1", "text": page.dialogue[0]["text"]},
                    ]),
                    "fake",
                    1,
                ))

            scheduler = ParallelPageScheduler(
                4,
                gpu_state="Cloud / Not Used",
                cancel_event=threading.Event(),
                snapshot_callback=snapshots.append,
            )
            outcomes = scheduler.run(jobs, stage, lambda result: committed.append(result.page_index))
            self.assertNotEqual(finished, [0, 1, 2, 3])
            self.assertEqual(committed, [0, 1, 2, 3])
            self.assertEqual([item.page_index for item in outcomes], [0, 1, 2, 3])
            self.assertTrue(all(item.active_workers <= 4 for item in snapshots))

    def test_failed_page_advances_ordered_cursor(self):
        with TemporaryDirectory() as raw:
            jobs = [_job(Path(raw), index) for index in range(3)]
            committed = []

            def stage(job):
                if job.page_index == 1:
                    raise RuntimeError("provider failed")
                return timed_stage(job, lambda page: (
                    PageTranslation(page.source_language, page.target_language, [{"id": "r1", "text": "ok"}]),
                    "fake",
                    1,
                ))

            ParallelPageScheduler(
                3, gpu_state="Unavailable", cancel_event=threading.Event(),
            ).run(jobs, stage, lambda result: committed.append((result.page_index, result.succeeded)))
            self.assertEqual(committed, [(0, True), (1, False), (2, True)])

    def test_cancellation_stops_dispatch_and_keeps_ordered_prefix(self):
        with TemporaryDirectory() as raw:
            jobs = [_job(Path(raw), index) for index in range(8)]
            cancel = threading.Event()
            committed = []

            def stage(job):
                time.sleep(0.01)
                if job.page_index == 0:
                    cancel.set()
                return timed_stage(job, lambda page: (
                    PageTranslation(page.source_language, page.target_language, [{"id": "r1", "text": "ok"}]),
                    "fake",
                    1,
                ))

            ParallelPageScheduler(
                2, gpu_state="Unavailable", cancel_event=cancel,
            ).run(jobs, stage, lambda result: committed.append(result.page_index))
            self.assertEqual(committed, list(range(len(committed))))
            self.assertLess(len(committed), len(jobs))


class _SessionManager:
    def __init__(self, failures: dict[str, int] | None = None):
        self.failures = failures or {}
        self.calls = []
        self.active = 0
        self.maximum_active = 0
        self.lock = threading.Lock()

    def translate_page_using(self, engine, page):
        with self.lock:
            self.calls.append(engine)
            self.active += 1
            self.maximum_active = max(self.maximum_active, self.active)
        try:
            remaining = self.failures.get(engine, 0)
            if remaining:
                self.failures[engine] = remaining - 1
                raise RuntimeError("temporary")
            time.sleep(0.01)
            return PageTranslation(page.source_language, page.target_language, [{"id": "r1", "text": "ok"}]), engine
        finally:
            with self.lock:
                self.active -= 1


class FastTranslationSessionTests(unittest.TestCase):
    def test_primary_retries_then_falls_back(self):
        manager = _SessionManager({"groq": 2})
        session = FastTranslationSession(
            manager,
            TranslationRuntimeConfig(preferred_engine="groq", fallback_engine="marian"),
        )
        result, provider, attempts = session.translate_page(
            PageDialogue("Japanese", "en", [{"id": "r1", "text": "x"}]),
        )
        self.assertEqual(provider, "marian")
        self.assertEqual(attempts, 3)
        self.assertEqual(manager.calls, ["groq", "groq", "marian"])
        self.assertEqual(result.translations[0]["text"], "ok")

    def test_local_inference_is_serialized(self):
        manager = _SessionManager()
        session = FastTranslationSession(
            manager,
            TranslationRuntimeConfig(preferred_engine="marian", fallback_engine=""),
        )
        page = PageDialogue("Japanese", "en", [{"id": "r1", "text": "x"}])
        threads = [threading.Thread(target=session.translate_page, args=(page,)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(manager.maximum_active, 1)

    def test_cloud_calls_can_overlap(self):
        manager = _SessionManager()
        session = FastTranslationSession(
            manager,
            TranslationRuntimeConfig(preferred_engine="groq", fallback_engine=""),
        )
        page = PageDialogue("Japanese", "en", [{"id": "r1", "text": "x"}])
        threads = [threading.Thread(target=session.translate_page, args=(page,)) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertGreater(manager.maximum_active, 1)

    def test_terminal_primary_failure_opens_health_circuit(self):
        class TerminalManager(_SessionManager):
            def translate_page_using(self, engine, page):
                self.calls.append(engine)
                if engine == "groq":
                    raise PermissionError("Unauthorized API key")
                return PageTranslation(page.source_language, page.target_language, [{"id": "r1", "text": "ok"}]), engine

        manager = TerminalManager()
        session = FastTranslationSession(
            manager,
            TranslationRuntimeConfig(preferred_engine="groq", fallback_engine="marian"),
        )
        page = PageDialogue("Japanese", "en", [{"id": "r1", "text": "x"}])
        session.translate_page(page)
        session.translate_page(page)
        self.assertEqual(manager.calls, ["groq", "marian", "marian"])


class ParallelSchedulerUiTests(unittest.TestCase):
    def test_fast_stats_are_visible_only_for_fast_project(self):
        from PySide6.QtWidgets import QApplication
        from hydra_manga_tl.project.model import MangaProject
        from hydra_manga_tl.project.workspace import WORKSPACE
        from hydra_manga_tl.ui.workspace import WorkspaceScreen

        app = QApplication.instance() or QApplication([])
        previous = WORKSPACE.current
        with TemporaryDirectory() as raw:
            project = MangaProject.create("Fast", Path(raw))
            project.quality = "Fast"
            WORKSPACE.current = project
            screen = WorkspaceScreen()
            try:
                screen._on_parallel_stats(SchedulerSnapshot(4, 3, 8, 2, 1, 12, "Active"))
                self.assertFalse(screen.parallel_status.isHidden())
                self.assertIn("Workers 3/4 active", screen.parallel_status.text())
                self.assertIn("GPU Active", screen.parallel_status.text())
                project.quality = "Balanced"
                screen._on_parallel_stats(SchedulerSnapshot(4, 0, 0, 12, 0, 12, "Idle"))
                self.assertTrue(screen.parallel_status.isHidden())
            finally:
                screen.deleteLater()
                app.processEvents()
                WORKSPACE.current = previous


if __name__ == "__main__":
    unittest.main()
