from __future__ import annotations

import os
import urllib.request
from pathlib import Path
from typing import Callable, Protocol

from hydra_manga_tl import __version__


class ModelDownloadError(RuntimeError):
    pass


class ModelDownloadCancelled(ModelDownloadError):
    pass


class _ReadableResponse(Protocol):
    def read(self, size: int = -1) -> bytes:
        ...


ProgressCallback = Callable[[int, int], None]
CancelCallback = Callable[[], bool]
Opener = Callable[[urllib.request.Request, float], _ReadableResponse]


def validate_gguf_model_file(path: str | Path, *, min_size_bytes: int = 1) -> None:
    model_path = Path(path)
    try:
        size = model_path.stat().st_size
    except OSError as error:
        raise ModelDownloadError(f"Model file is unavailable: {model_path}") from error
    if size < max(1, int(min_size_bytes)):
        raise ModelDownloadError("Downloaded model file is smaller than expected.")
    try:
        with model_path.open("rb") as handle:
            header = handle.read(4)
    except OSError as error:
        raise ModelDownloadError("Downloaded model file could not be read.") from error
    if header != b"GGUF":
        raise ModelDownloadError("Downloaded file is not a GGUF model.")


def download_model_file(
    *,
    url: str,
    destination: str | Path,
    min_size_bytes: int,
    opener: Opener | None = None,
    progress: ProgressCallback | None = None,
    cancel_requested: CancelCallback | None = None,
    remove_partial_on_cancel: bool = False,
    timeout: float = 30.0,
    chunk_size: int = 1024 * 1024,
) -> Path:
    text_url = str(url or "").strip()
    if not text_url.lower().startswith("https://"):
        raise ModelDownloadError("Model download URL must use HTTPS.")
    final_path = Path(destination)
    final_path.parent.mkdir(parents=True, exist_ok=True)
    if final_path.exists():
        validate_gguf_model_file(final_path, min_size_bytes=min_size_bytes)
        return final_path

    part_path = final_path.with_name(f"{final_path.name}.part")
    resume_from = _resume_size(part_path)
    headers = {"User-Agent": f"HydraMangaTL/{__version__}"}
    if resume_from > 0:
        headers["Range"] = f"bytes={resume_from}-"

    request = urllib.request.Request(
        text_url,
        headers=headers,
    )

    def default_opener(req: urllib.request.Request, seconds: float) -> _ReadableResponse:
        return urllib.request.urlopen(req, timeout=seconds)

    open_response = opener or default_opener
    bytes_done = 0
    total_bytes = 0

    try:
        response = open_response(request, timeout)
        try:
            range_accepted = resume_from > 0 and _is_partial_response(response)
            if resume_from > 0 and not range_accepted:
                part_path.unlink(missing_ok=True)
                resume_from = 0
            response_size = _content_length(response)
            total_bytes = resume_from + response_size if range_accepted else response_size
            bytes_done = resume_from
            if progress is not None and bytes_done:
                progress(bytes_done, total_bytes)
            mode = "ab" if range_accepted else "wb"
            with part_path.open(mode) as handle:
                while True:
                    if cancel_requested is not None and cancel_requested():
                        raise ModelDownloadCancelled("Model download was cancelled.")
                    chunk = response.read(chunk_size)
                    if not chunk:
                        break
                    handle.write(chunk)
                    bytes_done += len(chunk)
                    if progress is not None:
                        progress(bytes_done, total_bytes)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
        validate_gguf_model_file(part_path, min_size_bytes=min_size_bytes)
        os.replace(part_path, final_path)
        return final_path
    except ModelDownloadCancelled:
        if remove_partial_on_cancel:
            _remove_file(part_path)
        raise
    except Exception as error:
        if _is_bad_gguf_error(error):
            _remove_file(part_path)
        raise


def remove_partial_model_download(destination: str | Path) -> None:
    final_path = Path(destination)
    _remove_file(final_path.with_name(f"{final_path.name}.part"))


def partial_model_download_size(destination: str | Path) -> int:
    return _file_size(Path(destination).with_name(f"{Path(destination).name}.part"))


def _content_length(response: object) -> int:
    headers = getattr(response, "headers", None)
    if headers is not None:
        try:
            return int(headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return 0
    getheader = getattr(response, "getheader", None)
    if callable(getheader):
        try:
            return int(getheader("Content-Length") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def _is_partial_response(response: object) -> bool:
    status = getattr(response, "status", None)
    if status is None:
        getcode = getattr(response, "getcode", None)
        if callable(getcode):
            status = getcode()
    try:
        return int(status) == 206
    except (TypeError, ValueError):
        return False


def _resume_size(path: Path) -> int:
    size = _file_size(path)
    if size <= 0:
        _remove_file(path)
        return 0
    if size >= 4:
        try:
            with path.open("rb") as handle:
                if handle.read(4) != b"GGUF":
                    _remove_file(path)
                    return 0
        except OSError:
            _remove_file(path)
            return 0
    return size


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _is_bad_gguf_error(error: Exception) -> bool:
    return "not a GGUF" in str(error)
