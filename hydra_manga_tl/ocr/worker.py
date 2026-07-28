"""Spawn-safe PaddleOCR worker entry point."""

from __future__ import annotations

from multiprocessing.connection import Connection
from pathlib import Path
import tempfile
import time
import traceback
from typing import Any

from PIL import Image, ImageDraw


def _rss_mb() -> float:
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / (1024 * 1024), 2)
    except Exception:
        return 0.0


def _send_state(connection: Connection, state: str, **payload: Any) -> None:
    connection.send({"ok": True, "event": "state", "state": state, "rss_mb": _rss_mb(), **payload})


def _write_warmup_image(path: Path) -> None:
    image = Image.new("RGB", (320, 140), "white")
    draw = ImageDraw.Draw(image)
    # Keep worker startup independent of Pillow's optional FreeType extension.
    # Older Pillow builds can hard-crash Python 3.12 while drawing TrueType text,
    # which closes the multiprocessing pipe before an exception can be reported.
    # Paddle only needs representative high-contrast strokes to initialize its
    # inference path; the warm-up sample does not need to contain readable text.
    for x in (54, 112, 170, 228):
        draw.rectangle((x, 30, x + 9, 110), fill="black")
        draw.rectangle((x - 17, 48, x + 28, 57), fill="black")
        draw.rectangle((x - 12, 82, x + 23, 91), fill="black")
    draw.line((36, 112, 278, 112), fill="black", width=7)
    image.save(path)


def run_ocr_worker(connection: Connection, warmup_languages: tuple[str, ...] = ("japan",)) -> None:
    """Own all Paddle objects and serve JSON-compatible OCR requests."""
    from hydra_manga_tl.ocr.manager import SmartOCRManager
    from hydra_manga_tl.ocr.runtime import get_ocr_engine

    engines: dict[tuple[str, ...], Any] = {}
    managers: dict[tuple[str, ...], Any] = {}
    try:
        warmup_started = time.perf_counter()
        _send_state(connection, "LOADING_MODEL")
        warmup_key = tuple(warmup_languages or ("japan",))
        warmup_engine = get_ocr_engine(warmup_key)
        engines[warmup_key] = warmup_engine
        managers[warmup_key] = SmartOCRManager(warmup_engine, get_ocr_engine)
        _send_state(connection, "WARMING")
        with tempfile.TemporaryDirectory(prefix="hydra-ocr-worker-warmup-") as folder:
            sample = Path(folder) / "warmup.png"
            _write_warmup_image(sample)
            warmup_engine.analyze(sample, warmup_key[0] if warmup_key else None)
        _send_state(connection, "READY", warmup_seconds=round(time.perf_counter() - warmup_started, 3))
        while True:
            request = connection.recv()
            command = request.get("command")
            if command == "shutdown":
                break
            if command == "ping":
                connection.send({"ok": True, "state": "READY", "rss_mb": _rss_mb()})
                continue
            if command == "metrics":
                connection.send({"ok": True, "state": "READY", "rss_mb": _rss_mb()})
                continue
            if command not in {"analyze_page", "analyze_selection"}:
                connection.send({"ok": False, "error": f"Unknown OCR worker command: {command}"})
                continue
            try:
                languages = tuple(request.get("languages") or ("japan",))
                engine = engines.get(languages)
                if engine is None:
                    engine = get_ocr_engine(languages)
                    engines[languages] = engine
                manager = managers.get(languages)
                if manager is None:
                    manager = SmartOCRManager(engine, get_ocr_engine)
                    managers[languages] = manager
                if command == "analyze_selection":
                    managed = manager.analyze_selection(
                        Path(request["image_path"]),
                        list(request["rect"]),
                        preferred_language=request.get("preferred_language"),
                        quality=request.get("quality", "Balanced"),
                    )
                else:
                    managed = manager.analyze_page(
                        Path(request["image_path"]),
                        preferred_language=request.get("preferred_language"),
                        quality=request.get("quality", "Balanced"),
                        auto_language_fallback=bool(request.get("auto_language_fallback", False)),
                    )
                connection.send({
                    "ok": True,
                    "ocr_result": managed.ocr_result.to_dict(),
                    "final_regions": managed.final_regions,
                    "rss_mb": _rss_mb(),
                })
            except BaseException as error:
                connection.send({
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                    "rss_mb": _rss_mb(),
                })
    except (EOFError, BrokenPipeError):
        pass
    except BaseException as error:
        try:
            connection.send({
                "ok": False,
                "state": "FAILED",
                "error": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(limit=8),
                "rss_mb": _rss_mb(),
            })
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        try:
            connection.close()
        except OSError:
            pass
