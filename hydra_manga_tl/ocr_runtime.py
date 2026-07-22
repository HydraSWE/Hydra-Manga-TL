"""Shared OCR runtime warmup for the desktop pipeline."""

from __future__ import annotations

import logging
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .ocr import PaddleOCREngine


LOGGER = logging.getLogger(__name__)
DEFAULT_WARMUP_LANGUAGES = ("japan",)

_ENGINES: dict[tuple[str, ...], PaddleOCREngine] = {}
_WARMUP_THREADS: list[threading.Thread] = []
_LOCK = threading.RLock()


def _language_key(languages: tuple[str, ...] | list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(str(language) for language in languages if language))


def get_ocr_engine(languages: tuple[str, ...] | list[str]) -> PaddleOCREngine:
    key = _language_key(languages)
    warmup_thread: threading.Thread | None = None
    with _LOCK:
        engine = _ENGINES.get(key)
        if engine is not None:
            return engine
        warmup_thread = next((
            thread for thread in _WARMUP_THREADS
            if thread.is_alive() and getattr(thread, "_hydra_ocr_key", None) == key
        ), None)
    if warmup_thread is not None:
        warmup_thread.join()
        with _LOCK:
            engine = _ENGINES.get(key)
            if engine is not None:
                return engine
    with _LOCK:
        engine = _ENGINES.get(key)
        if engine is not None:
            return engine
        engine = PaddleOCREngine(list(key))
        _ENGINES[key] = engine
        return engine


def start_ocr_warmup(languages: tuple[str, ...] | list[str] = DEFAULT_WARMUP_LANGUAGES) -> None:
    key = _language_key(languages)
    with _LOCK:
        if key in _ENGINES:
            return
        if any(thread.is_alive() and getattr(thread, "_hydra_ocr_key", None) == key for thread in _WARMUP_THREADS):
            return
        thread = threading.Thread(target=_warm_ocr_engine, args=(key,), name=f"HydraOCRWarmup-{'+'.join(key)}", daemon=True)
        thread._hydra_ocr_key = key  # type: ignore[attr-defined]
        _WARMUP_THREADS.append(thread)
        thread.start()


def wait_for_ocr_warmup(timeout: float | None = None) -> bool:
    deadline = None if timeout is None else time.perf_counter() + timeout
    for thread in list(_WARMUP_THREADS):
        remaining = None if deadline is None else max(0.0, deadline - time.perf_counter())
        thread.join(remaining)
    with _LOCK:
        return DEFAULT_WARMUP_LANGUAGES in _ENGINES


def shutdown_ocr_warmup(timeout: float = 1.0) -> None:
    deadline = time.perf_counter() + timeout
    for thread in list(_WARMUP_THREADS):
        if not thread.is_alive():
            continue
        thread.join(max(0.0, deadline - time.perf_counter()))
    with _LOCK:
        _ENGINES.clear()
    PaddleOCREngine.clear_shared_engines()


def _warm_ocr_engine(key: tuple[str, ...]) -> None:
    started = time.perf_counter()
    try:
        engine = PaddleOCREngine(list(key))
        with tempfile.TemporaryDirectory(prefix="hydra-ocr-warmup-") as folder:
            sample = Path(folder) / "warmup.png"
            _write_warmup_image(sample)
            engine.analyze(sample, key[0] if key else None)
        with _LOCK:
            _ENGINES[key] = engine
        LOGGER.info("OCR warmup finished for %s in %.2fs", ",".join(key), time.perf_counter() - started)
    except Exception:
        LOGGER.exception("OCR warmup failed for %s", ",".join(key))


def _write_warmup_image(path: Path) -> None:
    image = Image.new("RGB", (320, 140), "white")
    draw = ImageDraw.Draw(image)
    font = _warmup_font(42)
    draw.text((32, 42), "テスト", fill="black", font=font)
    image.save(path)


def _warmup_font(size: int):
    for candidate in [
        Path(r"C:\Windows\Fonts\YuGothM.ttc"),
        Path(r"C:\Windows\Fonts\msgothic.ttc"),
        Path(r"C:\Windows\Fonts\meiryo.ttc"),
    ]:
        if candidate.exists():
            try:
                return ImageFont.truetype(str(candidate), size)
            except OSError:
                pass
    return ImageFont.load_default()
