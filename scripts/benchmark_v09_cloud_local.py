from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import sys
import time
from typing import Any


QWEN_DEFAULTS = {
    "QWEN_N_CTX": "2048",
    "QWEN_N_BATCH": "128",
    "QWEN_N_UBATCH": "64",
    "QWEN_N_GPU_LAYERS": "-1",
    "QWEN_FLASH_ATTN": "on",
    "QWEN_OFFLOAD_KQV": "on",
    "QWEN_OP_OFFLOAD": "on",
    "QWEN_TYPE_K": "q4_0",
    "QWEN_TYPE_V": "q4_0",
    "QWEN_N_THREADS": "4",
    "QWEN_N_THREADS_BATCH": "4",
    "QWEN_VERBOSE": "false",
}

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _shared_ocr(items: list[dict[str, str]], root: Path) -> dict[str, Any]:
    from hydra_manga_tl.ocr.service import OCRService
    from hydra_manga_tl.phase.preprocessor import prepare_ocr_image

    root.mkdir(parents=True, exist_ok=True)
    service = OCRService(
        ("japan",),
        use_subprocess=True,
        recycle_pages=25,
        memory_limit_mb=2048,
        retry_stats_path=root / "ocr_retry_stats.json",
    )
    pages = []
    started = time.perf_counter()
    try:
        for position, item in enumerate(items, 1):
            page_started = time.perf_counter()
            image_id = item["id"]
            source = Path(item["source_path"])
            print(f"OCR [{position}/{len(items)}] {source.name}", flush=True)
            preprocess_started = time.perf_counter()
            preprocessed = prepare_ocr_image(source, root / image_id / "preprocess")
            preprocess_seconds = time.perf_counter() - preprocess_started
            ocr_started = time.perf_counter()
            result = service.analyze_page(
                Path(preprocessed.ocr_path),
                preferred_language="japan",
                quality="Balanced",
                auto_language_fallback=False,
            )
            ocr_seconds = time.perf_counter() - ocr_started
            checkpoint = root / f"{image_id}_ocr.json"
            _write_json(checkpoint, result.ocr_result.to_dict())
            manager = result.ocr_result.metadata.get("manager", {})
            pages.append({
                "image_id": image_id,
                "source": str(source),
                "checkpoint": str(checkpoint),
                "preprocess_seconds": round(preprocess_seconds, 3),
                "ocr_seconds": round(ocr_seconds, 3),
                "total_seconds": round(time.perf_counter() - page_started, 3),
                "region_count": len(result.final_regions),
                "retry": manager.get("retry_summary", {}),
                "telemetry": result.telemetry,
            })
    finally:
        restart_count = service.restart_count
        service.close()
    summary = {
        "engine": "PaddleOCR subprocess",
        "quality": "Balanced",
        "total_seconds": round(time.perf_counter() - started, 3),
        "worker_restart_count": restart_count,
        "pages": pages,
    }
    _write_json(root / "ocr_summary.json", summary)
    return summary


def _run_engine(
    engine: str,
    items: list[dict[str, str]],
    root: Path,
    shared_ocr_root: Path,
) -> dict[str, Any]:
    from hydra_manga_tl.core.paths import PATHS
    from hydra_manga_tl.phase.pipeline import PipelineWorker
    from hydra_manga_tl.core.settings import SETTINGS

    artifacts = root / engine
    artifacts.mkdir(parents=True, exist_ok=True)
    for item in items:
        shutil.copy2(
            shared_ocr_root / f"{item['id']}_ocr.json",
            artifacts / f"{item['id']}_ocr.json",
        )

    original_ocr_cache = PATHS.ocr_cache
    original_translation_cache = PATHS.page_translation_cache
    PATHS.ocr_cache = root / "isolated_cache" / engine / "ocr"
    PATHS.page_translation_cache = root / "isolated_cache" / engine / "translation"

    failures: list[dict[str, str]] = []
    completed: list[dict[str, Any]] = []
    cancelled_state: list[bool] = []
    config = {
        "source_language": "Japanese",
        "quality": "Balanced",
        "literal_provider": "marian",
        "localization_provider": "local",
        "localization_style": "Manga",
        "text_style": "Manga",
        "bubble_padding": 5,
        "max_lines": 3,
        "glossary": {},
        "translation_engine": engine,
        "translation_fallback_engine": "",
        "qwen_model_path": SETTINGS.qwen_model_path,
        "qwen_model_name": SETTINGS.qwen_model_name,
        "provider_models": {"groq": SETTINGS.groq_model, "gemini": SETTINGS.gemini_model},
        "debug_artifacts_enabled": False,
        "ocr_subprocess_enabled": True,
        "ocr_worker_recycle_pages": 25,
        "ocr_worker_memory_limit_mb": 2048,
        "streaming_enabled": True,
        "translation_concurrency": 2,
    }
    worker = PipelineWorker(items, artifacts, "en", __import__("threading").Event(), config)
    worker.stage.connect(
        lambda image_id, stage, current, total, message: print(
            f"{engine.upper()} [{current}/{total}] {stage}: {Path(message).name if message else image_id}",
            flush=True,
        )
    )
    worker.image_finished.connect(lambda image_id, result: completed.append({"image_id": image_id, **result}))
    worker.image_failed.connect(lambda image_id, error: failures.append({"image_id": image_id, "error": error}))
    worker.finished.connect(lambda cancelled: cancelled_state.append(bool(cancelled)))
    started = time.perf_counter()
    try:
        worker.run()
    finally:
        PATHS.ocr_cache = original_ocr_cache
        PATHS.page_translation_cache = original_translation_cache
    elapsed = time.perf_counter() - started
    timing_path = artifacts / "pipeline_timing_summary.json"
    timing = json.loads(timing_path.read_text(encoding="utf-8")) if timing_path.is_file() else {}
    summary = {
        "engine": engine,
        "total_seconds_observed": round(elapsed, 3),
        "pipeline_timing": timing,
        "completed_count": len(completed),
        "failed_count": len(failures),
        "cancelled": cancelled_state[-1] if cancelled_state else False,
        "completed": completed,
        "failures": failures,
        "artifacts": str(artifacts.resolve()),
    }
    _write_json(artifacts / "benchmark_summary.json", summary)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=Path("samples/Manga/Cool3"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--start", type=int, default=19)
    parser.add_argument("--end", type=int, default=23)
    parser.add_argument("--ocr-only", action="store_true")
    args = parser.parse_args()
    for key, value in QWEN_DEFAULTS.items():
        os.environ.setdefault(key, value)

    if args.end < args.start:
        raise ValueError("--end must be greater than or equal to --start")
    sources = [args.source / f"{number}.webp" for number in range(args.start, args.end + 1)]
    missing = [str(path) for path in sources if not path.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing benchmark inputs: {missing}")
    items = [{"id": f"cool3_{path.stem}", "source_path": str(path.resolve())} for path in sources]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = (
        args.output
        or Path("outputs") / f"v09_cloud_local_cool3_{args.start}_{args.end}_{stamp}"
    ).resolve()
    root.mkdir(parents=True, exist_ok=False)
    print(f"BENCHMARK_ROOT={root}", flush=True)

    shared = _shared_ocr(items, root / "shared_ocr")
    if args.ocr_only:
        final = {
            "inputs": [str(path.resolve()) for path in sources],
            "quality": "Balanced",
            "ocr": shared,
        }
        _write_json(root / "comparison.json", final)
        print(f"COMPARISON={root / 'comparison.json'}", flush=True)
        return 0
    groq = _run_engine("groq", items, root, root / "shared_ocr")
    qwen = _run_engine("qwen", items, root, root / "shared_ocr")
    final = {
        "inputs": [str(path.resolve()) for path in sources],
        "quality": "Balanced",
        "ocr": shared,
        "cloud": groq,
        "local": qwen,
    }
    _write_json(root / "comparison.json", final)
    print(f"COMPARISON={root / 'comparison.json'}", flush=True)
    return 0 if not groq["failures"] and not qwen["failures"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
