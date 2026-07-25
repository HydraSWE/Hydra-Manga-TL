"""Unified OCR boundary for cache, retry policy, telemetry, and isolation."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from .ocr import OCRResult
from .ocr_manager import SmartOCRManager
from .ocr_runtime import OCRWorkerCrashed, get_ocr_engine, get_ocr_engine_for_language, get_ocr_runtime_client


def current_rss_mb() -> float:
    try:
        import psutil
        return round(psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024), 2)
    except Exception:
        return 0.0


@dataclass
class OCRServiceResult:
    ocr_result: OCRResult
    final_regions: list[dict[str, Any]]
    cache_hit: bool
    checkpoint: str
    telemetry: dict[str, Any]


class OCRRetryStatsStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()

    def record(self, manager_metadata: dict[str, Any]) -> None:
        attempts = list(manager_metadata.get("attempts", []))
        if not attempts:
            return
        with self._lock:
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                payload = {"version": 1, "reasons": {}}
            reasons = payload.setdefault("reasons", {})
            for attempt in attempts:
                reason = str(attempt.get("reason", "unknown"))
                bucket = reasons.setdefault(reason, {
                    "attempts": 0, "accepted": 0, "rejected": 0,
                    "total_score_delta": 0.0, "total_runtime_seconds": 0.0,
                })
                bucket["attempts"] += 1
                bucket["accepted" if attempt.get("accepted") else "rejected"] += 1
                bucket["total_score_delta"] += float(attempt.get("score_delta", 0.0) or 0.0)
                bucket["total_runtime_seconds"] += float(attempt.get("runtime_seconds", 0.0) or 0.0)
                count = max(1, int(bucket["attempts"]))
                bucket["average_score_delta"] = round(bucket["total_score_delta"] / count, 4)
                bucket["average_runtime_seconds"] = round(bucket["total_runtime_seconds"] / count, 4)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)


def _write_ocr_json(path: Path, result: OCRResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def regions_with_review_metadata(result: OCRResult) -> list[dict[str, Any]]:
    regions = list(result.to_dict().get("regions", []))
    queue = result.metadata.get("manager", {}).get("review_queue", [])
    for item in queue:
        region_id = str(item.get("region_id", ""))
        if not region_id.startswith("r") or not region_id[1:].isdigit():
            continue
        index = int(region_id[1:]) - 1
        if 0 <= index < len(regions):
            regions[index]["ocr_review_reasons"] = list(item.get("reasons", []))
    return regions


class OCRService:
    """The only page OCR boundary used by the batch pipeline."""

    def __init__(
        self,
        languages: tuple[str, ...],
        *,
        use_subprocess: bool = False,
        recycle_pages: int = 25,
        memory_limit_mb: int = 2048,
        retry_stats_path: Path | None = None,
    ) -> None:
        self.languages = tuple(languages)
        self.use_subprocess = bool(use_subprocess)
        self.worker = get_ocr_runtime_client() if self.use_subprocess else None
        if self.worker is not None:
            self.worker.memory_limit_mb = max(0, int(memory_limit_mb))
            self.worker.recycle_pages = max(1, int(recycle_pages))
        self._engine = None
        self._manager = None
        self.stats = OCRRetryStatsStore(retry_stats_path) if retry_stats_path else None

    @property
    def restart_count(self) -> int:
        return self.worker.restart_count if self.worker else 0

    def analyze_page(
        self,
        image_path: Path,
        *,
        preferred_language: str | None,
        quality: str,
        auto_language_fallback: bool,
        cache_path: Path | None = None,
        checkpoint_path: Path | None = None,
    ) -> OCRServiceResult:
        started = time.perf_counter()
        for checkpoint, path in (("project", checkpoint_path), ("global", cache_path)):
            if path is not None and path.is_file():
                result = OCRResult.from_dict(json.loads(path.read_text(encoding="utf-8")))
                result.metadata = {**result.metadata, "cache_hit": True, "cache_path": str(path), "checkpoint": checkpoint}
                return OCRServiceResult(
                    result, regions_with_review_metadata(result), True, checkpoint,
                    self._telemetry(time.perf_counter() - started),
                )

        if self.worker is not None:
            response = self._worker_request_with_retry("analyze_page", {
                "image_path": str(image_path),
                "languages": self.languages,
                "preferred_language": preferred_language,
                "quality": quality,
                "auto_language_fallback": auto_language_fallback,
            })
            result = OCRResult.from_dict(response["ocr_result"])
            final_regions = list(response["final_regions"])
        else:
            if self._engine is None:
                self._engine = get_ocr_engine(self.languages)
                self._manager = SmartOCRManager(self._engine, get_ocr_engine)
            managed = self._manager.analyze_page(
                image_path,
                preferred_language=preferred_language,
                quality=quality,
                auto_language_fallback=auto_language_fallback,
            )
            result, final_regions = managed.ocr_result, managed.final_regions

        result.metadata = {**result.metadata, "cache_hit": False, "cache_path": str(cache_path or "")}
        if cache_path is not None:
            _write_ocr_json(cache_path, result)
        if self.stats is not None:
            self.stats.record(dict(result.metadata.get("manager", {})))
        return OCRServiceResult(
            result, final_regions, False, "", self._telemetry(time.perf_counter() - started),
        )

    def _telemetry(self, elapsed: float) -> dict[str, Any]:
        runtime_metrics = self.worker.metrics() if self.worker else {}
        return {
            "service_seconds": round(elapsed, 3),
            "pipeline_rss_mb": current_rss_mb(),
            "worker_rss_mb": self.worker.worker_rss_mb if self.worker else 0.0,
            "worker_restart_count": self.restart_count,
            "runtime": runtime_metrics,
            "subprocess": self.worker is not None,
        }

    def analyze_selection(
        self,
        image_path: Path,
        rect: list[int],
        *,
        preferred_language: str,
        quality: str,
        cache_path: Path | None = None,
    ) -> OCRServiceResult:
        started = time.perf_counter()
        if cache_path is not None and cache_path.is_file():
            result = OCRResult.from_dict(json.loads(cache_path.read_text(encoding="utf-8")))
            return OCRServiceResult(
                result, regions_with_review_metadata(result), True, "selection",
                self._telemetry(time.perf_counter() - started),
            )
        if self.worker is not None:
            response = self._worker_request_with_retry("analyze_selection", {
                "image_path": str(image_path),
                "rect": [int(value) for value in rect],
                "languages": self.languages,
                "preferred_language": preferred_language,
                "quality": quality,
            })
            result = OCRResult.from_dict(response["ocr_result"])
            final_regions = list(response["final_regions"])
        else:
            if self._engine is None:
                self._engine = get_ocr_engine_for_language(preferred_language, self.languages)
                self._manager = SmartOCRManager(self._engine, get_ocr_engine)
            managed = self._manager.analyze_selection(
                image_path, rect, preferred_language=preferred_language, quality=quality,
            )
            result, final_regions = managed.ocr_result, managed.final_regions
        if cache_path is not None:
            _write_ocr_json(cache_path, result)
        if self.stats is not None:
            self.stats.record(dict(result.metadata.get("manager", {})))
        return OCRServiceResult(
            result, final_regions, False, "", self._telemetry(time.perf_counter() - started),
        )

    def close(self) -> None:
        # The subprocess worker is application-owned. Services borrow it and
        # release no shared OCR resources on page, batch, or manual completion.
        pass

    def _worker_request_with_retry(self, command: str, request: dict) -> dict:
        if self.worker is None:
            raise RuntimeError("OCR worker is not configured")
        try:
            return self.worker.request(command, request)
        except OCRWorkerCrashed as first_error:
            try:
                return self.worker.request(command, request)
            except OCRWorkerCrashed as second_error:
                raise OCRWorkerCrashed(f"{second_error}; first attempt: {first_error}") from second_error
