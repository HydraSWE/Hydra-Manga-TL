"""Build-time DLL path bootstrap for PyInstaller isolated child processes."""

from __future__ import annotations

import os
from pathlib import Path


def _add_dll_directory(path: str) -> None:
    folder = Path(path)
    if not folder.is_dir():
        return
    normalized = str(folder.resolve())
    try:
        os.add_dll_directory(normalized)
    except (AttributeError, FileNotFoundError, OSError):
        pass
    os.environ["PATH"] = normalized + os.pathsep + os.environ.get("PATH", "")


for value in os.environ.get("HYDRA_BUILD_DLL_DIRS", "").split(os.pathsep):
    if value:
        _add_dll_directory(value)
