"""Durable diagnostics for uncaught Python and native runtime failures."""

from __future__ import annotations

import atexit
from dataclasses import asdict
from datetime import datetime, timezone
import faulthandler
from importlib import metadata
import json
import logging
from pathlib import Path
import platform
import re
import sys
import threading
from types import TracebackType
from typing import Any, IO
import zipfile

from hydra_manga_tl.core.gpu import collect_gpu_diagnostics


LOGGER = logging.getLogger("hydra.crash")
_FAULT_FILE: IO[str] | None = None
_ORIGINAL_SYS_EXCEPTHOOK = sys.excepthook
_ORIGINAL_THREAD_EXCEPTHOOK = threading.excepthook
_INSTALLED = False
_SENSITIVE_KEY_PARTS = ("secret", "token", "password", "credential", "api_key")
_LOG_REDACTIONS = (
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer\s+)?)[^\s,;]+"),
    re.compile(r"(?i)((?:api[_ -]?key|token|password)\s*[=:]\s*)[^\s,;]+"),
    re.compile(r"(?i)([?&](?:key|api_key|token)=)[^&\s]+"),
)


def _log_unhandled(
    exception_type: type[BaseException],
    exception: BaseException,
    traceback: TracebackType | None,
    *,
    context: str,
) -> None:
    if issubclass(exception_type, KeyboardInterrupt):
        return
    LOGGER.critical(
        "Unhandled exception in %s",
        context,
        exc_info=(exception_type, exception, traceback),
    )


def _sys_excepthook(
    exception_type: type[BaseException],
    exception: BaseException,
    traceback: TracebackType | None,
) -> None:
    _log_unhandled(
        exception_type,
        exception,
        traceback,
        context="main thread",
    )
    _ORIGINAL_SYS_EXCEPTHOOK(exception_type, exception, traceback)


def _thread_excepthook(args: threading.ExceptHookArgs) -> None:
    thread_name = args.thread.name if args.thread is not None else "unknown thread"
    _log_unhandled(
        args.exc_type,
        args.exc_value,
        args.exc_traceback,
        context=f"thread {thread_name}",
    )
    _ORIGINAL_THREAD_EXCEPTHOOK(args)


def install_exception_logging(log_directory: Path) -> Path:
    """Install process-wide hooks after the normal application log is ready."""

    global _FAULT_FILE, _INSTALLED
    log_directory.mkdir(parents=True, exist_ok=True)
    fault_path = log_directory / "native_fault.log"
    if _FAULT_FILE is None or _FAULT_FILE.closed:
        _FAULT_FILE = fault_path.open("a", encoding="utf-8")
        try:
            faulthandler.enable(file=_FAULT_FILE, all_threads=True)
        except (OSError, RuntimeError):
            LOGGER.exception("Unable to enable native fault logging")
    if not _INSTALLED:
        sys.excepthook = _sys_excepthook
        threading.excepthook = _thread_excepthook
        _INSTALLED = True
    return fault_path


def _safe_metadata(value: Any, *, key: str = "") -> Any:
    if any(part in key.lower() for part in _SENSITIVE_KEY_PARTS):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(item_key): _safe_metadata(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe_metadata(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _redact_log_text(text: str) -> str:
    for pattern in _LOG_REDACTIONS:
        text = pattern.sub(r"\1[redacted]", text)
    return text


def _runtime_inventory() -> dict[str, Any]:
    packages = {}
    for package in (
        "PySide6",
        "Pillow",
        "numpy",
        "opencv-python",
        "paddleocr",
        "paddlepaddle",
        "torch",
        "llama-cpp-python",
    ):
        try:
            packages[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            packages[package] = "not installed"
    try:
        gpu = collect_gpu_diagnostics(run_load_test=False).to_dict()
    except Exception as error:
        gpu = {"error": f"{type(error).__name__}: {error}"}
    return {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "application_frozen": bool(getattr(sys, "frozen", False)),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "packages": packages,
        "gpu": gpu,
    }


def create_diagnostics_bundle(
    destination: Path,
    *,
    log_directory: Path,
    settings: Any,
    project_artifacts: Path | None = None,
) -> Path:
    """Write a support archive without credentials, source images, or renders."""

    destination = destination.with_suffix(".zip")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".zip.tmp")
    if temporary.exists():
        temporary.unlink()

    settings_payload = (
        asdict(settings) if hasattr(settings, "__dataclass_fields__") else dict(settings)
    )
    with zipfile.ZipFile(
        temporary,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
    ) as archive:
        archive.writestr(
            "settings.json",
            json.dumps(_safe_metadata(settings_payload), ensure_ascii=False, indent=2),
        )
        archive.writestr(
            "runtime_inventory.json",
            json.dumps(_runtime_inventory(), ensure_ascii=False, indent=2),
        )
        if log_directory.is_dir():
            for path in sorted(log_directory.glob("*.log"))[-10:]:
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                archive.writestr(f"logs/{path.name}", _redact_log_text(content))
        if project_artifacts is not None and project_artifacts.is_dir():
            timing_paths = sorted(
                (
                    path for path in project_artifacts.rglob("*.json")
                    if "timing" in path.name.lower()
                ),
                key=lambda path: path.stat().st_mtime,
            )[-20:]
            for path in timing_paths:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    relative = path.relative_to(project_artifacts)
                except (OSError, ValueError):
                    continue
                archive.writestr(
                    f"timings/{relative.as_posix()}",
                    json.dumps(_safe_metadata(payload), ensure_ascii=False, indent=2),
                )
    temporary.replace(destination)
    return destination.resolve()


def _shutdown_fault_logging() -> None:
    global _FAULT_FILE
    if _FAULT_FILE is None:
        return
    try:
        if faulthandler.is_enabled():
            faulthandler.disable()
    finally:
        _FAULT_FILE.close()
        _FAULT_FILE = None


atexit.register(_shutdown_fault_logging)
