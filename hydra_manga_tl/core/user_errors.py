"""User-facing error messages.

Technical exception details belong in logs, reports, and diagnostics. The
helpers here keep normal UI copy concise and actionable.
"""

from __future__ import annotations

import json
import re

from hydra_manga_tl.project.compatibility import (
    IncompatibleProjectError,
    InvalidProjectError,
    ProjectCompatibilityError,
)


def _raw_message(error: BaseException | str | None) -> str:
    if error is None:
        return ""
    return str(error).strip()


def _lower_message(error: BaseException | str | None) -> str:
    return _raw_message(error).lower()


def _looks_technical(message: str) -> bool:
    if not message:
        return False
    technical_patterns = (
        r"\b[A-Za-z_][A-Za-z0-9_]*(Error|Exception)\b",
        r"\btraceback\b",
        r"\bpid=\d+",
        r"\bexitcode=",
        r"\bstate=[A-Z_]+",
        r"\bsubprocess\b",
        r"\bstderr\b",
        r"\bstdout\b",
        r"\bline \d+\b",
        r"[A-Za-z]:\\",
    )
    return any(re.search(pattern, message, re.IGNORECASE) for pattern in technical_patterns)


def _is_permission_error(error: BaseException | str | None) -> bool:
    lowered = _lower_message(error)
    return isinstance(error, PermissionError) or any(
        token in lowered
        for token in (
            "permission denied",
            "access is denied",
            "not writable",
            "read-only",
            "readonly",
            "locked",
            "being used by another process",
        )
    )


def _is_missing_path_error(error: BaseException | str | None) -> bool:
    lowered = _lower_message(error)
    return isinstance(error, FileNotFoundError) or any(
        token in lowered
        for token in ("no such file", "not found", "cannot find the path", "does not exist")
    )


def _is_space_error(error: BaseException | str | None) -> bool:
    lowered = _lower_message(error)
    return any(token in lowered for token in ("no space", "disk full", "not enough space"))


def manual_translation_error(error: BaseException | str | None) -> str:
    message = _raw_message(error)
    lowered = message.lower()
    if "ocr worker" in lowered and (
        "warm-up" in lowered
        or "warmup" in lowered
        or "brokenpipeerror" in lowered
        or "eoferror" in lowered
        or "pipe has been ended" in lowered
    ):
        return (
            "OCR is still starting or restarted unexpectedly. "
            "Please wait a moment and try the manual text box again."
        )
    if "ocr worker" in lowered:
        return "OCR could not process the selected text. Please try again or restart Hydra Manga TL."
    if "qwen worker" in lowered or "qwen gguf" in lowered or "local qwen" in lowered:
        return (
            "Local translation failed. Try Marian fallback, lower local model settings, "
            "or restart the local model runtime."
        )
    if not message or _looks_technical(message):
        return "Manual translation failed. Please try again or use another translation engine."
    return message


def render_error(error: BaseException | str | None) -> str:
    if isinstance(error, MemoryError):
        return (
            "Hydra could not rerender this page because the original image exceeded available memory. "
            "Close other jobs or use a lower-resolution copy of this page."
        )
    if _is_permission_error(error):
        return "Hydra could not update the rendered page. Check that the project folder is writable."
    if _is_missing_path_error(error):
        return "Hydra could not find a required project file. Reload the project and try again."
    message = _raw_message(error)
    if not message or _looks_technical(message):
        return "Hydra could not rerender this page. Check the logs for details, then try again."
    return f"Hydra could not rerender this page: {message}"


def export_error(error: BaseException | str | None) -> str:
    if _is_space_error(error):
        return "Hydra could not finish the export because the destination drive is out of space."
    if _is_permission_error(error):
        return "Hydra could not write to the export folder. Choose another folder or check permissions."
    if _is_missing_path_error(error):
        return "Hydra could not find one of the files needed for export. Reload the project and try again."
    return "Hydra could not finish the export. Check that the destination folder is writable and has enough space."


def import_error(error: BaseException | str | None) -> str:
    if _is_permission_error(error):
        return "Hydra could not read those files. Check folder permissions and try again."
    if _is_missing_path_error(error):
        return "Hydra could not find the selected file or folder. Choose it again and retry."
    return "Hydra could not import those images. Check that the files are readable image files."


def project_open_error(error: BaseException | str | None) -> str:
    if isinstance(error, IncompatibleProjectError):
        return _raw_message(error) or "This project requires a newer Hydra Manga TL release."
    if isinstance(error, (InvalidProjectError, ProjectCompatibilityError, json.JSONDecodeError)):
        return (
            "Hydra could not open this project. The project file may be missing, "
            "damaged, or from an unsupported version."
        )
    if _is_permission_error(error):
        return "Hydra could not read this project. Check folder permissions and try again."
    if _is_missing_path_error(error):
        return "Hydra could not find the project file. Choose the project folder again."
    return (
        "Hydra could not open this project. The project file may be missing, "
        "damaged, or from an unsupported version."
    )


def pipeline_error(error: BaseException | str | None) -> str:
    message = _raw_message(error)
    lowered = message.lower()
    if "qwen" in lowered or "local model" in lowered:
        return (
            "Translation failed on this page. Try Marian fallback, lower local model settings, "
            "or retry the page."
        )
    if "ocr worker" in lowered or "paddleocr" in lowered:
        return "OCR failed on this page. Retry the page, or restart Hydra if OCR is still warming up."
    if _is_permission_error(error):
        return "Hydra could not write this page's results. Check that the project folder is writable."
    if _is_missing_path_error(error):
        return "Hydra could not find a file needed for this page. Reload the project and retry."
    if not message or _looks_technical(message):
        return "Hydra could not process this page. Check the logs, then retry the page."
    return f"Hydra could not process this page: {message}"


def settings_error(error: BaseException | str | None, *, target: str = "settings") -> str:
    if _is_permission_error(error):
        return f"Hydra could not save {target}. Check folder permissions and try again."
    if _is_missing_path_error(error):
        return f"Hydra could not find the selected {target} location. Choose another location."
    return f"Hydra could not save {target}. Check the selected value and try again."


def workspace_action_error(error: BaseException | str | None, *, action: str) -> str:
    if _is_permission_error(error):
        return f"Hydra could not {action}. Check that the project folder is writable."
    if _is_missing_path_error(error):
        return f"Hydra could not {action} because a project file is missing. Reload the project and try again."
    if isinstance(error, json.JSONDecodeError) or "json" in _lower_message(error):
        return f"Hydra could not {action} because the project data could not be read. Reload the project and try again."
    return f"Hydra could not {action}. Try again, or reload the project if the problem continues."


def data_folder_error(error: BaseException | str | None, folder_label: str) -> str:
    if _is_permission_error(error):
        return f"Hydra could not use that {folder_label}. Choose another folder or check permissions."
    if _is_missing_path_error(error):
        return f"Hydra could not find that {folder_label}. Choose another folder."
    return f"Hydra could not use that {folder_label}. Choose another folder and try again."


def memory_transfer_error(error: BaseException | str | None, *, action: str, memory_name: str) -> str:
    if _is_permission_error(error):
        return f"Hydra could not {action} {memory_name}. Check file permissions and try again."
    if _is_missing_path_error(error):
        return f"Hydra could not find the selected {memory_name} file. Choose it again and retry."
    return f"Hydra could not {action} {memory_name}. Check that the selected file is valid and writable."


def diagnostics_error(error: BaseException | str | None) -> str:
    if _is_permission_error(error):
        return "Hydra could not create the diagnostics bundle. Choose a writable folder."
    if _is_space_error(error):
        return "Hydra could not create the diagnostics bundle because the destination drive is out of space."
    return "Hydra could not create the diagnostics bundle. Choose another folder and try again."


def hydra_ai_error(error: BaseException | str | None) -> str:
    message = _raw_message(error)
    if not message or _looks_technical(message):
        return "Hydra AI is unavailable right now. Try again after restarting Hydra Manga TL."
    return message
