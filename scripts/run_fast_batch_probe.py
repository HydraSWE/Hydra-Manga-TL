from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QCoreApplication

from hydra_manga_tl.core.paths import PATHS
from hydra_manga_tl.core.settings import SETTINGS
from hydra_manga_tl.phase.pipeline import PipelineWorker


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"}


def _images(source: Path) -> list[Path]:
    return [
        item
        for item in sorted(source.iterdir(), key=lambda path: path.name.casefold())
        if item.is_file() and item.suffix.casefold() in IMAGE_SUFFIXES
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--engine", default="groq")
    parser.add_argument("--fallback", default="marian")
    args = parser.parse_args()

    app = QCoreApplication.instance() or QCoreApplication([])
    PATHS.initialize()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(PATHS.logs / "fast_batch_probe.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )

    images = _images(args.source)
    if args.limit > 0:
        images = images[: args.limit]
    if not images:
        raise SystemExit(f"No images found in {args.source}")

    items = [
        {"id": f"probe-{index:04d}", "source_path": str(path)}
        for index, path in enumerate(images, 1)
    ]
    config = {
        "project_id": f"fast-probe-{int(time.time())}",
        "quality": "Fast",
        "source_language": "Japanese",
        "target_language": "en",
        "translation_engine": args.engine,
        "translation_fallback_engine": args.fallback,
        "literal_provider": SETTINGS.literal_provider,
        "localization_provider": args.engine,
        "localization_model": SETTINGS.model_for(args.engine),
        "localization_style": "Manga",
        "text_style": "Manga",
        "auto_fit": True,
        "bubble_padding": SETTINGS.bubble_padding if hasattr(SETTINGS, "bubble_padding") else 5,
        "max_lines": SETTINGS.max_lines if hasattr(SETTINGS, "max_lines") else 3,
        "glossary": {},
        "debug_artifacts_enabled": SETTINGS.debug_artifacts_enabled,
        "ocr_subprocess_enabled": SETTINGS.ocr_subprocess_enabled,
        "ocr_worker_recycle_pages": SETTINGS.ocr_worker_recycle_pages,
        "ocr_worker_memory_limit_mb": SETTINGS.ocr_worker_memory_limit_mb,
        "fast_worker_override": SETTINGS.fast_worker_override,
        "translation_memory_enabled": SETTINGS.translation_memory_enabled,
        "translation_memory_prefer_verified": SETTINGS.translation_memory_prefer_verified,
        "translate_title": SETTINGS.translate_titles,
        "translate_sfx": SETTINGS.translate_sfx,
        "translate_sign": SETTINGS.translate_signs,
        "translate_credit": SETTINGS.translate_credits,
        "provider_models": {
            "groq": SETTINGS.groq_model,
            "gemini": SETTINGS.gemini_model,
        },
        "qwen_model_path": SETTINGS.qwen_model_path,
        "qwen_model_name": SETTINGS.qwen_model_name,
    }
    artifact_root = args.artifact_root.resolve()
    artifact_root.mkdir(parents=True, exist_ok=True)
    worker = PipelineWorker(items, artifact_root, "en", threading.Event(), config)

    started = time.perf_counter()
    counters = {"finished": 0, "failed": 0}

    def stage(image_id: str, phase: str, current: int, total: int, message: str) -> None:
        if phase in {"queued", "OCR", "translating", "rendering", "reconstructing", "ready", "failed"}:
            elapsed = time.perf_counter() - started
            print(f"[{elapsed:8.1f}s] stage {phase} {current}/{total}: {message}", flush=True)

    def snapshot(value) -> None:
        elapsed = time.perf_counter() - started
        translation_total = int(getattr(value, "translation_total", 0) or 0)
        if translation_total:
            print(
                f"[{elapsed:8.1f}s] smart units "
                f"{getattr(value, 'translation_done', 0)}/{translation_total} "
                f"queue={getattr(value, 'queued', 0)} "
                f"pages={getattr(value, 'completed', 0)}/{getattr(value, 'total', 0)} "
                f"calls={getattr(value, 'provider_calls', 0)} "
                f"retries={getattr(value, 'retries', 0)} "
                f"provider={getattr(value, 'active_provider', '')}",
                flush=True,
            )

    def finished(image_id: str, result: dict) -> None:
        counters["finished"] += 1
        elapsed = time.perf_counter() - started
        print(f"[{elapsed:8.1f}s] finished {counters['finished']}/{len(items)} {Path(result.get('rendered_image', '')).name}", flush=True)

    def failed(image_id: str, message: str) -> None:
        counters["failed"] += 1
        elapsed = time.perf_counter() - started
        print(f"[{elapsed:8.1f}s] failed {image_id}: {message}", flush=True)

    worker.stage.connect(stage)
    worker.scheduler_snapshot.connect(snapshot)
    worker.image_finished.connect(finished)
    worker.image_failed.connect(failed)

    cancelled = worker._run_fast()
    elapsed = time.perf_counter() - started
    print(
        f"DONE cancelled={cancelled} finished={counters['finished']} "
        f"failed={counters['failed']} elapsed={elapsed:.1f}s "
        f"artifacts={artifact_root}",
        flush=True,
    )
    app.processEvents()
    return 1 if cancelled or counters["failed"] else 0


if __name__ == "__main__":
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    raise SystemExit(main())
