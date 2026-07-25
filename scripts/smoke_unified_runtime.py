"""Disposable runtime smoke for the unified translation pipeline.

This script never writes to the source project. It creates a temporary
single-page project that references one existing source image, exercises the
real OCR/translation/render runtimes, and removes its project artifacts when
the process exits.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hydra_manga_tl.ocr_runtime import (  # noqa: E402
    get_ocr_runtime_metrics,
    shutdown_ocr_runtime,
    start_ocr_runtime,
)
from hydra_manga_tl.project import ImageRecord, MangaProject  # noqa: E402
from hydra_manga_tl.render_queue import (  # noqa: E402
    RENDER_QUEUE,
    shutdown_render_queue,
)
from hydra_manga_tl.settings import SETTINGS  # noqa: E402
from hydra_manga_tl.state import APP_STATE  # noqa: E402
from hydra_manga_tl.translation_queue import shutdown_translation_queue  # noqa: E402
from hydra_manga_tl.translation_requests import RenderRequest  # noqa: E402
from hydra_manga_tl.translation_runtime import (  # noqa: E402
    TRANSLATION_RUNTIME,
    shutdown_translation_runtime,
)
from hydra_manga_tl.workspace import WorkspaceManager  # noqa: E402
from hydra_manga_tl.paths import AppPaths  # noqa: E402
from PySide6.QtCore import QCoreApplication, QTimer  # noqa: E402
from PIL import Image  # noqa: E402


def _wait_for(app: QCoreApplication, predicate, timeout: float, label: str) -> None:
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    app.processEvents()
    if not predicate():
        raise TimeoutError(f"Timed out waiting for {label}")


def _manual_rect(translation_result: Path, source: Path) -> list[int]:
    payload = json.loads(translation_result.read_text(encoding="utf-8"))
    with Image.open(source) as opened:
        width, height = opened.size
    candidates = [
        group for group in payload.get("translation_groups", [])
        if group.get("polygon")
        and str(group.get("original_text") or "").strip()
        and group.get("type") in {"dialogue", "narration"}
    ]
    if not candidates:
        candidates = [
            group for group in payload.get("translation_groups", [])
            if group.get("polygon") and str(group.get("original_text") or "").strip()
        ]
    if not candidates:
        raise RuntimeError("No translated text group is available for manual smoke")
    polygon = candidates[0]["polygon"]
    xs = [int(point[0]) for point in polygon]
    ys = [int(point[1]) for point in polygon]
    padding = 8
    rect = [
        max(0, min(xs) - padding),
        max(0, min(ys) - padding),
        min(width, max(xs) + padding),
        min(height, max(ys) + padding),
    ]
    if rect[2] - rect[0] < 8 or rect[3] - rect[1] < 8:
        raise RuntimeError(f"Derived manual rectangle is too small: {rect}")
    return rect


def _copy_project_config(
    source: MangaProject,
    root: Path,
    image_index: int,
    image_count: int,
) -> MangaProject:
    project = MangaProject.create(f"{source.name} UTP smoke", root)
    for field in (
        "target_language",
        "source_language",
        "quality",
        "literal_provider",
        "localization_provider",
        "localization_model",
        "localization_style",
        "text_style",
        "auto_fit",
        "bubble_padding",
        "max_lines",
        "glossary",
    ):
        setattr(project, field, getattr(source, field))
    project.glossary = dict(project.glossary)
    project.glossary["__HYDRA_UTP_SMOKE__"] = str(uuid4())
    project.images = [
        ImageRecord(
            id=str(uuid4()),
            source_path=source_image.source_path,
            relative_path=source_image.relative_path,
            status="queued",
        )
        for source_image in source.images[image_index:image_index + image_count]
    ]
    project.save()
    return project


def run(
    project_path: Path,
    image_index: int,
    timeout: float,
    engine: str | None = None,
    image_count: int = 1,
) -> dict:
    app = QCoreApplication.instance() or QCoreApplication([])
    if engine:
        SETTINGS.translation_engine = engine.strip().lower()
    source_project = MangaProject.load(project_path)
    if not (0 <= image_index < len(source_project.images)):
        raise IndexError(f"Image index {image_index} is outside the source project")
    if image_count < 1 or image_index + image_count > len(source_project.images):
        raise IndexError(
            f"Image range {image_index}:{image_index + image_count} "
            "is outside the source project"
        )
    source = Path(source_project.images[image_index].source_path)
    if not source.is_file():
        raise FileNotFoundError(source)

    report: dict = {
        "source_project": source_project.name,
        "source_image": source.name,
        "source_images": [
            Path(item.source_path).name
            for item in source_project.images[image_index:image_index + image_count]
        ],
        "translation_engine": SETTINGS.translation_engine,
        "checks": {},
        "events": [],
    }

    with tempfile.TemporaryDirectory(prefix="hydra-utp-smoke-") as temporary:
        smoke_root = Path(temporary)
        paths = AppPaths(smoke_root / "app")
        paths.initialize()
        project = _copy_project_config(
            source_project, smoke_root / "project", image_index, image_count,
        )
        manager = WorkspaceManager(paths=paths)
        manager._set_current(project)
        pipeline_outcome: list[bool] = []
        manual_done: list[tuple[int, str]] = []
        manual_failed: list[tuple[int, str]] = []
        image_events: list[tuple[str, float]] = []
        task_events: list[tuple[str, str, str]] = []
        manager.pipeline_finished.connect(pipeline_outcome.append)
        manager.manual_region_finished.connect(
            lambda index, key: manual_done.append((index, key)),
        )
        manager.manual_region_failed.connect(
            lambda index, message: manual_failed.append((index, message)),
        )
        manager.image_updated.connect(
            lambda index: image_events.append(("image_updated", time.monotonic())),
        )
        manager.translation_request_state_changed.connect(
            lambda request_id, state, message: task_events.append(
                (request_id, state, message),
            ),
        )

        start_ocr_runtime(memory_limit_mb=SETTINGS.ocr_worker_memory_limit_mb)
        report["ocr_before"] = get_ocr_runtime_metrics()

        image_id = project.images[0].id
        if not manager.start_pipeline({image_id}):
            raise RuntimeError("Translate Selected smoke request was not accepted")
        _wait_for(
            app,
            lambda: bool(pipeline_outcome) and not manager.pipeline.running,
            timeout,
            "Translate Selected",
        )
        if pipeline_outcome[-1]:
            raise RuntimeError("Translate Selected was cancelled")
        translated_image = project.images[0]
        if translated_image.status not in {"ready", "review"}:
            raise RuntimeError(
                f"Translate Selected ended with status {translated_image.status}: "
                f"{translated_image.error}"
            )
        translation_result = Path(translated_image.translation_result)
        rendered_image = Path(translated_image.rendered_image)
        if not translation_result.is_file() or not rendered_image.is_file():
            raise RuntimeError("Translate Selected did not create its result artifacts")

        report["checks"]["translate_selected"] = True
        report["checks"]["selected_request_type"] = any(
            request_id.startswith("selected:") and state == "done"
            for request_id, state, _ in task_events
        )
        generation_after_selected = TRANSLATION_RUNTIME.generation
        if generation_after_selected < 1:
            raise RuntimeError("Shared translation manager was not created")

        rect = _manual_rect(translation_result, source)
        report["manual_rect"] = rect
        blocker_release = threading.Event()
        blocker_started = threading.Event()

        def block_render(result_path, render_dir, policy):
            blocker_started.set()
            if not blocker_release.wait(timeout):
                raise TimeoutError("Smoke render blocker was not released")
            return {"blocked": True}

        blocker = RenderRequest(
            request_id=f"smoke-blocker:{uuid4()}",
            project_id=project.id,
            image_id=image_id,
            image_index=0,
            result_path=translation_result,
            render_dir=project.artifacts / "smoke-blocker",
            source_path=source,
            reason="review",
        )
        blocker_future = RENDER_QUEUE.submit(blocker, block_render)
        if not blocker_started.wait(2):
            raise RuntimeError("Render queue blocker did not start")

        heartbeat = {"ticks": 0}
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(
            lambda: heartbeat.__setitem__("ticks", heartbeat["ticks"] + 1),
        )
        timer.start()
        generation_before_manual = TRANSLATION_RUNTIME.generation
        if not manager.request_manual_region(0, rect):
            blocker_release.set()
            raise RuntimeError("Manual request was not accepted")

        queued_depth = {"value": 0}
        deadline = time.monotonic() + timeout
        while not manual_done and not manual_failed and time.monotonic() < deadline:
            app.processEvents()
            if any(state == "rendering" for _, state, _ in task_events):
                pending = RENDER_QUEUE.pending_count
                queued_depth["value"] = max(queued_depth["value"], pending)
                if pending >= 2:
                    blocker_release.set()
            time.sleep(0.01)
        blocker_release.set()
        blocker_future.result(timeout=5)
        timer.stop()
        app.processEvents()
        if manual_failed:
            raise RuntimeError(f"Manual request failed: {manual_failed[-1][1]}")
        if not manual_done:
            raise TimeoutError("Timed out waiting for manual request")
        if len(project.images[0].manual_regions) != 1:
            raise RuntimeError("Successful manual request did not persist one region")

        report["checks"]["ui_event_loop_responsive"] = heartbeat["ticks"] > 0
        report["checks"]["render_was_queued"] = queued_depth["value"] >= 2
        report["checks"]["one_translation_manager"] = (
            generation_before_manual == TRANSLATION_RUNTIME.generation
        )
        report["translation_runtime_generation"] = TRANSLATION_RUNTIME.generation

        render_json = (
            project.artifacts
            / image_id
            / f"{source.stem}_render.json"
        )
        render_payload = json.loads(render_json.read_text(encoding="utf-8"))
        rendered_groups = render_payload.get("rendered_groups", [])
        exact = [
            group for group in rendered_groups
            if group.get("placement_strategy") == "manual_exact_bounds"
        ]
        if not exact:
            raise RuntimeError("Manual render did not use manual_exact_bounds")
        box = [int(value) for value in exact[-1].get("box", [])]
        if len(box) != 4 or not (
            rect[0] <= box[0] <= box[2] <= rect[2]
            and rect[1] <= box[1] <= box[3] <= rect[3]
        ):
            raise RuntimeError(
                f"Manual rendered box {box} escaped selected rectangle {rect}"
            )
        report["checks"]["manual_exact_bounds"] = True

        states = [state for _, state, _ in task_events]
        report["checks"]["refresh_after_render"] = (
            "rendering" in states
            and "done" in states
            and bool(image_events)
        )

        previous_regions = len(project.images[0].manual_regions)
        failure_count = len(manual_failed)
        original_renderer = manager._run_editor_render

        def forced_failure(result_path, render_dir, policy):
            raise RuntimeError("forced smoke render failure")

        manager._run_editor_render = forced_failure
        try:
            if not manager.request_manual_region(0, rect):
                raise RuntimeError("Forced-failure manual request was not accepted")
            _wait_for(
                app,
                lambda: len(manual_failed) > failure_count,
                timeout,
                "forced manual render failure",
            )
        finally:
            manager._run_editor_render = original_renderer
        if len(project.images[0].manual_regions) != previous_regions:
            raise RuntimeError("Manual render failure did not roll back project state")
        report["checks"]["render_failure_rollback"] = True

        if image_count > 1:
            previous_outcomes = len(pipeline_outcome)
            for image in project.images:
                image.status = "queued"
                image.error = ""
            project.save()
            if not manager.start_pipeline():
                raise RuntimeError("Mixed-cache batch smoke request was not accepted")
            _wait_for(
                app,
                lambda: (
                    len(pipeline_outcome) > previous_outcomes
                    and not manager.pipeline.running
                ),
                timeout,
                "mixed-cache multi-page batch",
            )
            if pipeline_outcome[-1]:
                raise RuntimeError("Mixed-cache batch was cancelled")
            failed_images = [
                image for image in project.images
                if image.status not in {"ready", "review"}
            ]
            if failed_images:
                raise RuntimeError(
                    "Mixed-cache batch failed: "
                    + ", ".join(
                        f"{Path(image.source_path).name}={image.status}:{image.error}"
                        for image in failed_images
                    )
                )
            timing_summary = json.loads(
                (project.artifacts / "pipeline_timing_summary.json").read_text(
                    encoding="utf-8",
                )
            )
            cache_hits = int(timing_summary.get("translation_cache_hits", 0))
            if not (0 < cache_hits < image_count):
                raise RuntimeError(
                    f"Expected mixed translation cache results, got "
                    f"{cache_hits}/{image_count} hits"
                )
            report["checks"]["mixed_cache_multi_page_batch"] = True
            report["checks"]["batch_request_type"] = any(
                request_id.startswith("batch:") and state == "done"
                for request_id, state, _ in task_events
            )
            report["translation_cache_hits"] = cache_hits
            report["translation_cache_misses"] = image_count - cache_hits
            report["review_queue_counts"] = {
                "ocr": len(manager.ocr_review_queue()),
                "translation": len(manager.review_issue_queue()),
            }
            report["checks"]["review_workflow_accessible"] = True
            report["checks"]["combined_session_workflows"] = all(
                report["checks"].get(name, False)
                for name in (
                    "translate_selected",
                    "batch_request_type",
                    "manual_exact_bounds",
                    "review_workflow_accessible",
                )
            )

        report["ocr_after"] = get_ocr_runtime_metrics()
        before_pid = report["ocr_before"].get("worker_pid")
        after_pid = report["ocr_after"].get("worker_pid")
        report["checks"]["one_ocr_runtime"] = (
            before_pid is not None and before_pid == after_pid
        )
        report["events"] = [
            {"request_id": request_id, "state": state, "message": message}
            for request_id, state, message in task_events
        ]
        report["temporary_project_removed_on_exit"] = True
        manager.shutdown()
        APP_STATE.reset()

    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--image-index", type=int, default=0)
    parser.add_argument("--image-count", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--engine", choices=["groq", "gemini", "deepseek", "marian", "qwen"])
    args = parser.parse_args()
    try:
        report = run(
            args.project,
            args.image_index,
            args.timeout,
            args.engine,
            args.image_count,
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    finally:
        shutdown_translation_queue()
        shutdown_render_queue()
        shutdown_translation_runtime()
        shutdown_ocr_runtime()


if __name__ == "__main__":
    raise SystemExit(main())
