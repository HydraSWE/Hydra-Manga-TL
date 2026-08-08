from __future__ import annotations

import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
import unittest
from unittest.mock import patch

from hydra_manga_tl.core.paths import AppPaths
from hydra_manga_tl.core.settings import AppSettings, CredentialStore
from hydra_manga_tl.phase.pipeline import (
    PipelineService,
    PipelineWorker,
    _bubble_display_id,
    _stable_bubble_id,
)
from hydra_manga_tl.project.artifacts import (
    rendered_filename,
    target_render_dir,
    target_translation_path,
)
from hydra_manga_tl.project.model import ImageRecord, MangaProject
from hydra_manga_tl.project.workspace import WorkspaceManager
from hydra_manga_tl.translation.engines import PageDialogue, PageTranslation
from hydra_manga_tl.translation.engines.remote_engine import (
    OpenAICompatiblePageEngine,
    OpenAIPageEngine,
    _post_chat_completion_stream,
)
from hydra_manga_tl.translation.cache_store import TranslationCacheStore
from hydra_manga_tl.translation.engines.registry import TRANSLATION_PROVIDER_REGISTRY
from hydra_manga_tl.translation.runtime import FastTranslationSession, TranslationRuntimeConfig
from hydra_manga_tl.translation.scheduler import (
    ParallelPageJob,
    ParallelPageScheduler,
    PageTranslationOutcome,
    ProviderProfile,
    ProviderDispatcher,
    SmartPageJob,
    SmartTranslationScheduler,
    SchedulerSnapshot,
    TokenBudgetManager,
    TranslationQueueItem,
    DEFAULT_PROVIDER_PROFILES,
    auto_worker_count,
    resolve_provider_worker_count,
    resolve_worker_count,
    timed_stage,
)
from hydra_manga_tl.translation.service import TranslationHTTPError


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
    def test_stable_bubble_identity_uses_project_image_and_ordinal(self):
        self.assertEqual(
            "workspace-1:image-2:b0003",
            _stable_bubble_id("workspace-1", "image-2", 3),
        )
        self.assertEqual("page_005/bubble_003", _bubble_display_id(5, 3))
        self.assertEqual(
            _stable_bubble_id("workspace-1", "image-2", 3),
            _stable_bubble_id("workspace-1", "image-2", 3),
        )

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
            stages = []
            worker.stage.connect(
                lambda image_id, stage, current, total, message: stages.append(
                    (image_id, stage, current, total, message)
                )
            )

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

            class Manager:
                def translate_page_using(self, engine, page):
                    return PageTranslation(
                        page.source_language,
                        page.target_language,
                        [
                            {"id": item["id"], "text": "ok"}
                            for item in page.dialogue
                        ],
                    ), engine

            fake_ocr = type("FakeOCR", (), {"close": lambda self: None})()
            fake_session = type("FakeSession", (), {
                "gpu_state": "Cloud / Not Used",
                "primary": "groq",
                "fallback": "",
                "manager": Manager(),
            })()
            with patch("hydra_manga_tl.phase.pipeline.OCRService", return_value=fake_ocr), \
                    patch.object(worker, "_prepare_fast_page", side_effect=prepare), \
                    patch.object(worker, "_commit_fast_outcome", side_effect=lambda outcome, _: committed.append(outcome.page_index) or {}), \
                    patch("hydra_manga_tl.phase.pipeline.TRANSLATION_RUNTIME.fast_session", return_value=fake_session):
                cancelled = worker._run_fast()
            self.assertFalse(cancelled)
            self.assertEqual(committed, [0, 1, 2, 3])
            queued = [item for item in stages if "smart translation" in item[4]]
            self.assertEqual([item[1] for item in queued], ["queued"] * 4)

    def test_openai_compatible_fast_streams_page_translations(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            items = [
                {"id": f"p{index}", "source_path": str(root / f"{index}.png")}
                for index in range(3)
            ]
            worker = PipelineWorker(items, root, "en", threading.Event(), {
                "quality": "Fast", "translation_engine": "openai_compatible",
            })
            committed = []
            stages = []
            worker.stage.connect(
                lambda image_id, stage, current, total, message: stages.append(
                    (image_id, stage, current, total, message)
                )
            )

            def prepare(item, position, total, ocr_service, requested, preferred, manifest):
                manifest.ensure_page(item["id"], item["source_path"])
                payload = {
                    "source": item["source_path"],
                    "source_language": "Japanese",
                    "dialogue": [{"id": "r1", "text": item["id"]}],
                    "timing": {"cache": {}, "stages": {}},
                    "item": item,
                    "position": position,
                }
                path = root / "fast_jobs" / f"{position - 1:06d}_{item['id']}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload), encoding="utf-8")
                return {
                    "path": str(path), "dialogue": payload["dialogue"],
                    "position": position, "image_id": item["id"],
                }

            class Session:
                primary = "openai_compatible"
                fallback = ""
                gpu_state = "Cloud / Not Used"

                def translate_page(self, page):
                    return (
                        PageTranslation(
                            page.source_language,
                            page.target_language,
                            [{"id": item["id"], "text": "ok"} for item in page.dialogue],
                        ),
                        self.primary,
                        1,
                    )

                def translate_cached_page(self, page, cached):
                    return cached, "page-cache", 1

            fake_ocr = type("FakeOCR", (), {"close": lambda self: None})()
            with patch("hydra_manga_tl.phase.pipeline.OCRService", return_value=fake_ocr), \
                    patch.object(worker, "_prepare_fast_page", side_effect=prepare), \
                    patch.object(worker, "_commit_fast_outcome", side_effect=lambda outcome, _: committed.append(outcome.page_index) or {}), \
                    patch("hydra_manga_tl.phase.pipeline.TRANSLATION_RUNTIME.fast_session", return_value=Session()), \
                    patch("hydra_manga_tl.phase.pipeline.SmartTranslationScheduler.run") as smart_run:
                cancelled = worker._run_fast()

            self.assertFalse(cancelled)
            smart_run.assert_not_called()
            self.assertEqual(committed, [0, 1, 2])
            self.assertFalse(any("smart translation" in item[4] for item in stages))
            self.assertEqual(
                [item[1] for item in stages if "page translation" in item[4]],
                ["queued", "queued", "queued"],
            )

    def test_openai_compatible_fast_logs_page_translation_failures(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            item = {"id": "p0", "source_path": str(root / "0.png")}
            worker = PipelineWorker([item], root, "en", threading.Event(), {
                "quality": "Fast", "translation_engine": "openai_compatible",
            })
            outcomes = []

            def prepare(item, position, total, ocr_service, requested, preferred, manifest):
                manifest.ensure_page(item["id"], item["source_path"])
                payload = {
                    "source": item["source_path"],
                    "source_language": "Japanese",
                    "dialogue": [{"id": "r1", "text": "待て"}],
                    "timing": {"cache": {}, "stages": {}},
                    "item": item,
                    "position": position,
                }
                path = root / "fast_jobs" / f"{position - 1:06d}_{item['id']}.json"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(payload), encoding="utf-8")
                return {
                    "path": str(path), "dialogue": payload["dialogue"],
                    "position": position, "image_id": item["id"],
                }

            class Session:
                primary = "openai_compatible"
                fallback = ""
                gpu_state = "Cloud / Not Used"

                def translate_page(self, page):
                    raise RuntimeError("router stalled")

            fake_ocr = type("FakeOCR", (), {"close": lambda self: None})()
            with patch("hydra_manga_tl.phase.pipeline.OCRService", return_value=fake_ocr), \
                    patch.object(worker, "_prepare_fast_page", side_effect=prepare), \
                    patch.object(worker, "_commit_fast_outcome", side_effect=lambda outcome, _: outcomes.append(outcome) or {}), \
                    patch("hydra_manga_tl.phase.pipeline.TRANSLATION_RUNTIME.fast_session", return_value=Session()), \
                    self.assertLogs("hydra_manga_tl.phase.pipeline", level="WARNING") as logs:
                cancelled = worker._run_fast()

            self.assertFalse(cancelled)
            self.assertEqual(len(outcomes), 1)
            self.assertIn("router stalled", outcomes[0].error)
            self.assertTrue(any("OpenAI-compatible page translation failed" in line for line in logs.output))

    def test_balanced_openai_compatible_translation_logs_runtime_boundary(self):
        worker = PipelineWorker([], Path("."), "en", threading.Event(), {
            "quality": "Balanced",
            "translation_engine": "openai_compatible",
            "provider_models": {"openai_compatible": "vendor/model"},
            "provider_base_urls": {"openai_compatible": "https://router.example/v1"},
        })
        page = PageDialogue("Japanese", "en", [{"id": "r1", "text": "待て"}])
        translated = PageTranslation("Japanese", "en", [{"id": "r1", "text": "Wait!"}])

        with patch(
            "hydra_manga_tl.phase.pipeline.TRANSLATION_RUNTIME.translate_page",
            return_value=translated,
        ), self.assertLogs("hydra_manga_tl.phase.pipeline", level="INFO") as logs:
            self.assertIs(worker._translate_page_dialogue(page), translated)

        joined = "\n".join(logs.output)
        self.assertIn("OpenAI-compatible pipeline translation started", joined)
        self.assertIn("OpenAI-compatible pipeline translation finished", joined)
        self.assertIn("https://router.example/v1", joined)

    def test_balanced_openai_compatible_translation_logs_runtime_failure(self):
        worker = PipelineWorker([], Path("."), "en", threading.Event(), {
            "quality": "Balanced",
            "translation_engine": "openai_compatible",
            "provider_models": {"openai_compatible": "vendor/model"},
            "provider_base_urls": {"openai_compatible": "https://router.example/v1"},
        })
        page = PageDialogue("Japanese", "en", [{"id": "r1", "text": "待て"}])

        with patch(
            "hydra_manga_tl.phase.pipeline.TRANSLATION_RUNTIME.translate_page",
            side_effect=RuntimeError("router stalled"),
        ), self.assertLogs("hydra_manga_tl.phase.pipeline", level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "router stalled"):
                worker._translate_page_dialogue(page)

        self.assertTrue(any("OpenAI-compatible pipeline translation failed" in line for line in logs.output))

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


class SmartTranslationSchedulerTests(unittest.TestCase):
    def _smart_job(self, root: Path, bubble_count: int = 4) -> SmartPageJob:
        page = PageDialogue(
            "Japanese",
            "en",
            [
                {
                    "id": f"r{index + 1}",
                    "bubble_id": f"project:image:b{index:04d}",
                    "text": f"一{index}",
                }
                for index in range(bubble_count)
            ],
        )
        return SmartPageJob(
            0,
            "image",
            "batch:image",
            root / "job.json",
            root / "legacy.json",
            root / "bubbles",
            page,
        )

    def test_token_budget_batches_do_not_exceed_profile_limits(self):
        manager = TokenBudgetManager(DEFAULT_PROVIDER_PROFILES["groq"])
        queued = [
            TranslationQueueItem(
                unit=None,
                estimated_tokens=value,
            )
            for value in (700, 700, 700)
        ]
        batch = manager.next_batch(queued)
        self.assertEqual([700, 700], [item.estimated_tokens for item in batch])
        self.assertLessEqual(sum(item.estimated_tokens for item in batch), 1800)

    def test_provider_worker_resolution_clamps_user_override(self):
        self.assertEqual(
            resolve_provider_worker_count(DEFAULT_PROVIDER_PROFILES["groq"], 0),
            1,
        )
        self.assertEqual(
            resolve_provider_worker_count(DEFAULT_PROVIDER_PROFILES["groq"], 6),
            2,
        )
        self.assertEqual(
            resolve_provider_worker_count(DEFAULT_PROVIDER_PROFILES["qwen"], 6),
            1,
        )
        self.assertEqual(
            resolve_provider_worker_count(DEFAULT_PROVIDER_PROFILES["marian"], 6),
            1,
        )
        self.assertEqual(
            resolve_provider_worker_count(DEFAULT_PROVIDER_PROFILES["gemini"], 4),
            4,
        )
        self.assertEqual(
            resolve_provider_worker_count(DEFAULT_PROVIDER_PROFILES["openai"], 4),
            4,
        )
        self.assertEqual(
            resolve_provider_worker_count(DEFAULT_PROVIDER_PROFILES["google"], 4),
            4,
        )
        self.assertEqual(
            resolve_provider_worker_count(DEFAULT_PROVIDER_PROFILES["deepseek"], 6),
            3,
        )
        self.assertEqual(
            resolve_provider_worker_count(DEFAULT_PROVIDER_PROFILES["openai_compatible"], 6),
            2,
        )

    def test_openai_providers_are_registered_and_cache_identity_includes_base_url(self):
        self.assertIn("openai", TRANSLATION_PROVIDER_REGISTRY)
        self.assertIn("openai_compatible", TRANSLATION_PROVIDER_REGISTRY)
        self.assertTrue(TRANSLATION_PROVIDER_REGISTRY["openai"].cloud)
        self.assertTrue(TRANSLATION_PROVIDER_REGISTRY["openai_compatible"].cloud)
        config = TranslationRuntimeConfig.from_mapping({
            "translation_engine": "openai_compatible",
            "provider_models": {"openai_compatible": "vendor/model"},
            "provider_base_urls": {"openai_compatible": "https://one.example/v1"},
        })
        self.assertEqual(
            dict(config.provider_base_urls),
            {"openai_compatible": "https://one.example/v1"},
        )
        page = PageDialogue(
            source_language="Japanese",
            target_language="en",
            dialogue=[{"id": "1", "text": "待て"}],
        )
        first = TranslationCacheStore.page_translation_key(
            page,
            {
                "translation_engine": "openai_compatible",
                "provider_models": {"openai_compatible": "vendor/model"},
                "provider_base_urls": {"openai_compatible": "https://one.example/v1"},
            },
            "en",
        )
        second = TranslationCacheStore.page_translation_key(
            page,
            {
                "translation_engine": "openai_compatible",
                "provider_models": {"openai_compatible": "vendor/model"},
                "provider_base_urls": {"openai_compatible": "https://two.example/v1"},
            },
            "en",
        )
        self.assertNotEqual(first, second)

    def test_openai_credentials_support_env_aliases(self):
        store = CredentialStore()
        with patch.dict(os.environ, {"OPENAI_API_KEY": "openai-key"}, clear=True):
            self.assertEqual(store.get("openai"), "openai-key")
        with patch.dict(os.environ, {"TOKENROUTER_API_KEY": "router-key"}, clear=True):
            self.assertEqual(store.get("openai_compatible"), "router-key")
        with patch.dict(
            os.environ,
            {
                "OPENAI_COMPATIBLE_API_KEY": "compatible-key",
                "TOKENROUTER_API_KEY": "router-key",
            },
            clear=True,
        ):
            self.assertEqual(store.get("openai_compatible"), "compatible-key")

    def test_openai_provider_settings_round_trip(self):
        with TemporaryDirectory() as folder:
            paths = AppPaths(Path(folder))
            AppSettings(
                app_data_root=str(paths.root),
                openai_model="gpt-test",
                openai_compatible_name="Router",
                openai_compatible_base_url="https://router.example/v1",
                openai_compatible_model="vendor/model",
            ).save(paths)

            loaded = AppSettings.load(paths)

        self.assertEqual(loaded.openai_model, "gpt-test")
        self.assertEqual(loaded.openai_compatible_name, "Router")
        self.assertEqual(loaded.openai_compatible_base_url, "https://router.example/v1")
        self.assertEqual(loaded.openai_compatible_model, "vendor/model")
        self.assertEqual(loaded.model_for("openai"), "gpt-test")
        self.assertEqual(loaded.model_for("openai_compatible"), "vendor/model")

    def test_openai_compatible_streams_with_longer_timeout_only_for_compatible_engine(self):
        page = PageDialogue("Japanese", "en", [{"id": "r1", "text": "待て"}])
        response = {
            "choices": [{
                "message": {
                    "content": json.dumps({
                        "translations": [{"id": "r1", "text": "Wait."}],
                    }),
                },
            }],
        }
        streamed = json.dumps({"translations": [{"id": "r1", "text": "Wait."}]})
        with patch(
            "hydra_manga_tl.translation.engines.remote_engine._post_chat_completion_stream",
            return_value=streamed,
        ) as post:
            OpenAICompatiblePageEngine(
                api_key="key",
                model="vendor/model",
                base_url="https://router.example/v1",
            ).translate_page(page)

        self.assertEqual(post.call_args.args[0], "https://router.example/v1/chat/completions")
        self.assertTrue(post.call_args.args[1]["stream"])
        self.assertEqual(post.call_args.args[1]["stream_options"], {"include_usage": True})
        self.assertNotIn("response_format", post.call_args.args[1])
        self.assertEqual(post.call_args.kwargs.get("timeout"), 120)

        with patch("hydra_manga_tl.translation.engines.remote_engine._post_json", return_value=response) as post:
            OpenAIPageEngine(api_key="key", model="gpt-test").translate_page(page)

        self.assertNotIn("timeout", post.call_args.kwargs)
        self.assertEqual(DEFAULT_PROVIDER_PROFILES["openai_compatible"].request_timeout, 120.0)

    def test_openai_compatible_stream_parser_collects_delta_content(self):
        class Response:
            def __enter__(self):
                return iter([
                    b'data: {"choices":[{"delta":{"content":"{\\"translations\\":["}}]}\n\n',
                    b'data: {"choices":[{"delta":{"content":"{\\"id\\":\\"r1\\",\\"text\\":\\"Wait.\\"}]"}}]}\n\n',
                    b'data: {"choices":[{"delta":{"content":"}"}}]}\n\n',
                    b"data: [DONE]\n\n",
                ])

            def __exit__(self, *_args):
                return False

        with patch("hydra_manga_tl.translation.engines.remote_engine.urlopen", return_value=Response()):
            self.assertEqual(
                _post_chat_completion_stream(
                    "https://router.example/v1/chat/completions",
                    {"stream": True},
                    headers={"Authorization": "Bearer key"},
                ),
                '{"translations":[{"id":"r1","text":"Wait."}]}',
            )

    def test_translate_pending_skips_cancelled_page_with_completed_outputs(self):
        class Event:
            def connect(self, callback):
                self.callback = callback

        class Queue:
            def __init__(self):
                self.failed = Event()
                self.requests = ()

            def submit_group(self, requests, handler):
                self.requests = tuple(requests)
                self.handler = handler
                return object()

        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "page.png"
            source.write_bytes(b"source")
            project = MangaProject(
                "project",
                "Chapter",
                str(root),
                images=[
                    ImageRecord(
                        "page-1",
                        str(source),
                        source.name,
                        status="cancelled",
                    ),
                ],
            )
            translation = target_translation_path(project.artifacts, "page-1", "en")
            translation.parent.mkdir(parents=True, exist_ok=True)
            translation.write_text(
                json.dumps({
                    "translation_groups": [{"status": "review"}],
                    "ai_review": {"issue_count": 0},
                }),
                encoding="utf-8",
            )
            render_dir = target_render_dir(project.artifacts, "page-1", "en")
            render_dir.mkdir(parents=True, exist_ok=True)
            rendered = render_dir / rendered_filename(source, "en")
            rendered.write_bytes(b"rendered")
            queue = Queue()
            service = PipelineService(queue)

            self.assertFalse(service.process_project(project))

        self.assertEqual((), queue.requests)
        self.assertEqual("review", project.images[0].status)
        self.assertEqual(str(translation), project.images[0].translation_result)
        self.assertEqual(str(rendered), project.images[0].rendered_image)

    def test_workspace_cancel_preserves_completed_review_pages(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            project = MangaProject(
                "project",
                "Chapter",
                str(root),
                images=[
                    ImageRecord("done", str(root / "done.png"), "done.png", status="review"),
                    ImageRecord("active", str(root / "active.png"), "active.png", status="translating"),
                    ImageRecord("queued", str(root / "queued.png"), "queued.png", status="queued"),
                    ImageRecord("partial", str(root / "partial.png"), "partial.png", status="partial"),
                ],
            )
            project.save()
            manager = WorkspaceManager(paths=AppPaths(root / "app"))
            manager.current = project
            manager._active_job_ids = ["done", "active", "queued", "partial"]

            manager._on_completed(True)

        self.assertEqual("review", project.images[0].status)
        self.assertEqual("cancelled", project.images[1].status)
        self.assertEqual("queued", project.images[2].status)
        self.assertEqual("partial", project.images[3].status)

    def test_translate_pending_skips_partial_pages(self):
        class Event:
            def connect(self, callback):
                self.callback = callback

        class Queue:
            def __init__(self):
                self.failed = Event()
                self.requests = ()

            def submit_group(self, requests, handler):
                self.requests = tuple(requests)
                self.handler = handler
                return object()

        with TemporaryDirectory() as folder:
            root = Path(folder)
            images = []
            for status in ("pending", "queued", "failed", "cancelled", "partial", "ready", "review"):
                source = root / f"{status}.png"
                source.write_bytes(b"source")
                images.append(ImageRecord(status, str(source), source.name, status=status))
            project = MangaProject("project", "Chapter", str(root), images=images)
            project.save()
            queue = Queue()
            manager = WorkspaceManager(paths=AppPaths(root / "app"), pipeline=PipelineService(queue))
            manager.current = project

            self.assertTrue(manager.start_pipeline())

        self.assertEqual(
            {request.request_id for request in queue.requests},
            {"batch:pending", "batch:queued", "batch:failed", "batch:cancelled"},
        )

    def test_retranslate_selected_queues_any_status_and_forces_pipeline(self):
        class Event:
            def connect(self, callback):
                self.callback = callback

        class Queue:
            def __init__(self):
                self.failed = Event()
                self.requests = ()

            def submit_group(self, requests, handler):
                self.requests = tuple(requests)
                self.handler = handler
                return object()

        with TemporaryDirectory() as folder:
            root = Path(folder)
            sources = []
            images = []
            for status in ("ready", "failed", "partial"):
                source = root / f"{status}.png"
                source.write_bytes(b"source")
                sources.append(source)
                images.append(ImageRecord(status, str(source), source.name, status=status))
            project = MangaProject("project", "Chapter", str(root), images=images)
            project.save()
            queue = Queue()
            manager = WorkspaceManager(paths=AppPaths(root / "app"), pipeline=PipelineService(queue))
            manager.current = project

            self.assertTrue(
                manager.start_pipeline(
                    {image.id for image in images},
                    retranslate=True,
                )
            )

        self.assertEqual({request.request_id for request in queue.requests}, {
            "selected:ready",
            "selected:failed",
            "selected:partial",
        })
        self.assertTrue(all(image.status == "queued" for image in project.images))

    def test_force_retranslate_clears_page_outputs_before_queueing(self):
        class Event:
            def connect(self, callback):
                self.callback = callback

        class Queue:
            def __init__(self):
                self.failed = Event()
                self.requests = ()

            def submit_group(self, requests, handler):
                self.requests = tuple(requests)
                self.handler = handler
                return object()

        class Paths:
            def __init__(self, root: Path):
                self.ocr_cache = root / "ocr-cache"
                self.page_translation_cache = root / "page-cache"
                self.ocr_cache.mkdir(parents=True)
                self.page_translation_cache.mkdir(parents=True)

        with TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "page.png"
            source.write_bytes(b"source")
            project = MangaProject(
                "project",
                "Chapter",
                str(root),
                images=[ImageRecord("page", str(source), source.name, status="ready")],
            )
            image = project.images[0]
            ocr_path = project.artifacts / f"{image.id}_ocr.json"
            translation = target_translation_path(project.artifacts, image.id, "en")
            render_dir = target_render_dir(project.artifacts, image.id, "en")
            rendered = render_dir / rendered_filename(source, "en")
            preview = render_dir / f"{source.stem}_preview.png"
            for path in (ocr_path, translation, rendered, preview):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"stale")
            image.ocr_result = str(ocr_path)
            image.translation_result = str(translation)
            image.rendered_image = str(rendered)
            image.preview_image = str(preview)
            queue = Queue()
            service = PipelineService(queue)
            service.set_force_retranslate(True)
            fake_paths = Paths(root / "cache")
            ocr_cache = fake_paths.ocr_cache / f"page_old.json"
            page_cache = fake_paths.page_translation_cache / f"page_old.json"
            ocr_cache.write_bytes(b"cache")
            page_cache.write_bytes(b"cache")

            with patch("hydra_manga_tl.phase.pipeline.PATHS", fake_paths):
                self.assertTrue(service.process_project(project, {image.id}))

        self.assertEqual(1, len(queue.requests))
        self.assertFalse(ocr_path.exists())
        self.assertFalse(translation.exists())
        self.assertFalse(render_dir.exists())
        self.assertFalse(ocr_cache.exists())
        self.assertFalse(page_cache.exists())
        self.assertEqual("", image.ocr_result)
        self.assertEqual("", image.translation_result)
        self.assertEqual("", image.rendered_image)
        self.assertEqual("queued", image.status)

    def test_smart_scheduler_honors_effective_provider_workers(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            job = self._smart_job(root, bubble_count=4)

            class Manager:
                def __init__(self):
                    self.active = 0
                    self.maximum_active = 0
                    self.lock = threading.Lock()

                def translate_page_using(self, engine, requested):
                    with self.lock:
                        self.active += 1
                        self.maximum_active = max(self.maximum_active, self.active)
                    try:
                        time.sleep(0.03)
                        return PageTranslation(
                            requested.source_language,
                            requested.target_language,
                            [
                                {"id": item["id"], "text": "ok"}
                                for item in requested.dialogue
                            ],
                        ), engine
                    finally:
                        with self.lock:
                            self.active -= 1

            manager = Manager()
            session = type("Session", (), {
                "manager": manager,
                "primary": "groq",
                "fallback": "",
            })()
            snapshots: list[SchedulerSnapshot] = []
            profile = ProviderProfile(
                "groq",
                "Groq",
                default_parallel=1,
                max_parallel=2,
                target_tokens=13,
                max_tokens=13,
            )
            scheduler = SmartTranslationScheduler(
                primary_provider="groq",
                profiles={"groq": profile},
                worker_override=6,
                cancel_event=threading.Event(),
                snapshot_callback=snapshots.append,
            )
            with patch("hydra_manga_tl.translation.scheduler.TRANSLATION_MEMORY.lookup", return_value=None):
                scheduler.run(
                    [job],
                    ProviderDispatcher(session),
                    lambda outcome: outcome,
                )
            self.assertEqual(manager.maximum_active, 2)
            self.assertTrue(snapshots)
            self.assertTrue(all(item.active_workers <= 2 for item in snapshots))
            self.assertTrue(any(item.configured_workers == 2 for item in snapshots))

    def test_rate_limit_reduces_budget_without_exceeding_worker_cap(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            job = self._smart_job(root, bubble_count=3)

            class Manager:
                def __init__(self):
                    self.calls = 0

                def translate_page_using(self, engine, requested):
                    self.calls += 1
                    if self.calls == 1:
                        raise TranslationHTTPError(429, "rate limit", retry_after=0)
                    return PageTranslation(
                        requested.source_language,
                        requested.target_language,
                        [
                            {"id": item["id"], "text": "ok"}
                            for item in requested.dialogue
                        ],
                    ), engine

            session = type("Session", (), {
                "manager": Manager(),
                "primary": "groq",
                "fallback": "",
            })()
            snapshots: list[SchedulerSnapshot] = []
            profile = ProviderProfile(
                "groq",
                "Groq",
                default_parallel=1,
                max_parallel=2,
                target_tokens=30,
                max_tokens=13,
                cooldown=0,
            )
            scheduler = SmartTranslationScheduler(
                primary_provider="groq",
                profiles={"groq": profile},
                worker_override=6,
                cancel_event=threading.Event(),
                snapshot_callback=snapshots.append,
            )
            with patch("hydra_manga_tl.translation.scheduler.TRANSLATION_MEMORY.lookup", return_value=None):
                outcomes = scheduler.run(
                    [job],
                    ProviderDispatcher(session),
                    lambda outcome: outcome,
                )
            self.assertTrue(outcomes[0].succeeded)
            self.assertEqual(scheduler.retries, 1)
            self.assertTrue(all(item.active_workers <= 2 for item in snapshots))
            budgets = [
                item.current_token_budget
                for item in snapshots
                if item.current_token_budget
            ]
            self.assertTrue(any(value < 30 for value in budgets))

    def test_smart_scheduler_publishes_provider_liveness_while_waiting(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            job = self._smart_job(root, bubble_count=2)

            class Manager:
                def translate_page_using(self, engine, requested):
                    time.sleep(0.05)
                    return PageTranslation(
                        requested.source_language,
                        requested.target_language,
                        [
                            {"id": item["id"], "text": "ok"}
                            for item in requested.dialogue
                        ],
                    ), engine

            session = type("Session", (), {
                "manager": Manager(),
                "primary": "groq",
                "fallback": "",
            })()
            snapshots: list[SchedulerSnapshot] = []
            profile = ProviderProfile(
                "groq",
                "Groq",
                default_parallel=1,
                max_parallel=1,
                target_tokens=40,
                max_tokens=40,
            )
            scheduler = SmartTranslationScheduler(
                primary_provider="groq",
                profiles={"groq": profile},
                cancel_event=threading.Event(),
                snapshot_callback=snapshots.append,
            )
            with patch("hydra_manga_tl.translation.scheduler.PROVIDER_FUTURE_POLL_SECONDS", 0.01), \
                    patch("hydra_manga_tl.translation.scheduler.TRANSLATION_MEMORY.lookup", return_value=None):
                outcomes = scheduler.run(
                    [job],
                    ProviderDispatcher(session),
                    lambda outcome: outcome,
                )
            self.assertTrue(outcomes[0].succeeded)
            self.assertTrue(any(item.provider_status == "sending" for item in snapshots))
            waiting = [item for item in snapshots if item.provider_status == "waiting"]
            self.assertTrue(waiting)
            self.assertTrue(any(item.in_flight_units == 2 for item in waiting))

    def test_smart_scheduler_maps_results_by_bubble_id(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            page = PageDialogue(
                "Japanese",
                "en",
                [
                    {"id": "r1", "bubble_id": "project:image:b0000", "text": "一"},
                    {"id": "r2", "bubble_id": "project:image:b0001", "text": "二"},
                ],
            )
            job = SmartPageJob(
                0,
                "image",
                "batch:image",
                root / "job.json",
                root / "legacy.json",
                root / "bubbles",
                page,
            )
            committed: list[PageTranslationOutcome] = []

            class Manager:
                def translate_page_using(self, engine, requested):
                    return PageTranslation(
                        requested.source_language,
                        requested.target_language,
                        [
                            {"id": "project:image:b0001", "text": "Two"},
                            {"id": "project:image:b0000", "text": "One"},
                        ],
                    ), "fake-provider"

            session = type("Session", (), {
                "manager": Manager(),
                "primary": "groq",
                "fallback": "",
            })()
            scheduler = SmartTranslationScheduler(
                primary_provider="groq",
                cancel_event=threading.Event(),
            )
            with patch("hydra_manga_tl.translation.scheduler.TRANSLATION_MEMORY.lookup", return_value=None):
                scheduler.run(
                    [job],
                    ProviderDispatcher(session),
                    lambda outcome: committed.append(outcome) or outcome,
                )
            self.assertEqual(["r1", "r2"], [item["id"] for item in committed[0].translation.translations])
            self.assertEqual(["One", "Two"], [item["text"] for item in committed[0].translation.translations])
            self.assertTrue(any((root / "bubbles").glob("*.json")))

    def test_smart_scheduler_reads_legacy_page_cache_without_provider_call(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            cache = root / "legacy.json"
            cache.write_text(
                '{"source_language":"Japanese","target_language":"en","translations":[{"id":"r1","text":"Cached"}]}',
                encoding="utf-8",
            )
            page = PageDialogue(
                "Japanese",
                "en",
                [{"id": "r1", "bubble_id": "project:image:b0000", "text": "一"}],
            )
            job = SmartPageJob(0, "image", "batch:image", root / "job.json", cache, root / "bubbles", page)

            class Manager:
                def translate_page_using(self, engine, requested):
                    raise AssertionError("provider should not be called")

            session = type("Session", (), {
                "manager": Manager(),
                "primary": "groq",
                "fallback": "",
            })()
            committed: list[PageTranslationOutcome] = []
            SmartTranslationScheduler(
                primary_provider="groq",
                cancel_event=threading.Event(),
            ).run(
                [job],
                ProviderDispatcher(session),
                lambda outcome: committed.append(outcome) or outcome,
            )
            self.assertEqual("Cached", committed[0].translation.translations[0]["text"])

    def test_smart_scheduler_commits_page_as_soon_as_units_are_ready(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            jobs = []
            for index in range(2):
                page = PageDialogue(
                    "Japanese",
                    "en",
                    [{
                        "id": "r1",
                        "bubble_id": f"project:image{index}:b0000",
                        "text": f"一{index}",
                    }],
                )
                jobs.append(SmartPageJob(
                    index,
                    f"image{index}",
                    f"batch:image{index}",
                    root / f"job{index}.json",
                    root / f"legacy{index}.json",
                    root / f"bubbles{index}",
                    page,
                ))
            committed: list[int] = []

            class OrderedDispatcher:
                def __init__(self):
                    self.calls = 0

                def dispatch(self, provider_key, units):
                    self.calls += 1
                    if self.calls == 2:
                        self_seen = list(committed)
                        if self_seen != [0]:
                            raise AssertionError(f"page 1 was not committed: {self_seen}")
                    return PageTranslation(
                        "Japanese",
                        "en",
                        [{"id": units[0].bubble_id, "text": "ok"}],
                    ), provider_key

            profile = ProviderProfile(
                "groq",
                "Groq",
                default_parallel=1,
                max_parallel=1,
                target_tokens=1,
                max_tokens=1,
            )
            scheduler = SmartTranslationScheduler(
                primary_provider="groq",
                profiles={"groq": profile},
                cancel_event=threading.Event(),
            )
            with patch("hydra_manga_tl.translation.scheduler.TRANSLATION_MEMORY.lookup", return_value=None):
                scheduler.run(
                    jobs,
                    OrderedDispatcher(),
                    lambda outcome: committed.append(outcome.page_index) or outcome,
                )
            self.assertEqual(committed, [0, 1])

    def test_smart_scheduler_times_out_hung_provider_batch(self):
        with TemporaryDirectory() as raw:
            root = Path(raw)
            job = self._smart_job(root, bubble_count=1)

            class HangingDispatcher:
                def dispatch(self, provider_key, units):
                    time.sleep(0.2)
                    return PageTranslation("Japanese", "en", []), provider_key

            profile = ProviderProfile(
                "groq",
                "Groq",
                default_parallel=1,
                max_parallel=1,
                request_timeout=0.01,
                cooldown=0,
            )
            committed: list[PageTranslationOutcome] = []
            scheduler = SmartTranslationScheduler(
                primary_provider="groq",
                profiles={"groq": profile},
                cancel_event=threading.Event(),
            )
            with patch("hydra_manga_tl.translation.scheduler.TRANSLATION_MEMORY.lookup", return_value=None):
                outcomes = scheduler.run(
                    [job],
                    HangingDispatcher(),
                    lambda outcome: committed.append(outcome) or outcome,
                )
            self.assertEqual(len(outcomes), 1)
            self.assertFalse(outcomes[0].succeeded)
            self.assertIn("Smart scheduler did not translate", outcomes[0].error)
            self.assertGreaterEqual(scheduler.retries, 1)


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
            TranslationRuntimeConfig(preferred_engine="gemini", fallback_engine=""),
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
            screen = WorkspaceScreen()
            if not hasattr(screen, "_on_parallel_stats"):
                self.skipTest("WorkspaceScreen does not include legacy parallel stats UI widgets in V1")
            try:
                screen._on_parallel_stats(SchedulerSnapshot(4, 3, 8, 2, 1, 12, "Active"))
                self.assertFalse(screen.parallel_status.isHidden())
                self.assertIn("Workers 3/4 active", screen.parallel_status.text())
                self.assertIn("GPU Active", screen.parallel_status.text())
                screen._on_parallel_stats(SchedulerSnapshot(
                    2, 1, 3, 0, 0, 4, "Cloud / Not Used",
                    translation_total=10,
                    translation_done=0,
                    active_provider="groq",
                    active_batches=1,
                    in_flight_units=10,
                    provider_status="sending",
                ))
                self.assertEqual(screen.progress.format(), "0.0%")
                self.assertIn("Dispatching 10 units to groq", screen.current_page_label.text())
                screen._on_pipeline("queued", 4, 4, "Queued page.webp for smart translation")
                self.assertIn("Dispatching 10 units to groq", screen.current_page_label.text())
                screen._advance_progress_animation()
                self.assertIn("Dispatching 10 units to groq", screen.current_page_label.text())
                self.assertNotIn("Page 1/4", screen.current_page_label.text())
                screen._on_parallel_stats(SchedulerSnapshot(
                    2, 1, 3, 0, 0, 4, "Cloud / Not Used",
                    translation_total=10,
                    translation_done=5,
                    active_provider="groq",
                    current_token_budget=1800,
                ))
                self.assertEqual(screen.progress.format(), "40.0%")
                self.assertIn("5/10 units", screen.current_page_label.text())
                self.assertIn("Translating with groq", screen.job_overall.text())
                screen._on_parallel_stats(SchedulerSnapshot(
                    2, 1, 3, 0, 0, 4, "Cloud / Not Used",
                    translation_total=10,
                    translation_done=5,
                    active_provider="groq",
                    current_token_budget=1800,
                    active_batches=1,
                    in_flight_units=3,
                    provider_status="waiting",
                ))
                self.assertIn("3 in flight", screen.current_page_label.text())
                self.assertIn("Waiting for groq", screen.job_overall.text())
                self.assertIn("Waiting", screen.parallel_status.text())
                self.assertIn("In flight 3", screen.parallel_status.text())
                project.quality = "Balanced"
                screen._on_parallel_stats(SchedulerSnapshot(4, 0, 0, 12, 0, 12, "Idle"))
                self.assertTrue(screen.parallel_status.isHidden())
            finally:
                screen.deleteLater()
                app.processEvents()
                WORKSPACE.current = previous


if __name__ == "__main__":
    unittest.main()
