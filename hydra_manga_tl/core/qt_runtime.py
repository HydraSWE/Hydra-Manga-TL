"""Windows DLL discovery helpers for Qt/PySide startup."""

from __future__ import annotations

import os
import site
import sys
from pathlib import Path


_CONFIGURED = False


def configure_qt_dll_paths() -> None:
    """Register Python, PySide6, shiboken, and Qt plugin folders before Qt imports."""
    global _CONFIGURED
    if _CONFIGURED or os.name != "nt":
        return

    candidates: list[Path] = [
        Path(sys.base_prefix),
        Path(sys.prefix),
        Path(sys.executable).resolve().parent,
    ]
    try:
        candidates.extend(Path(path) for path in site.getsitepackages())
    except Exception:
        pass

    dll_dirs: list[Path] = []
    pyside_roots: list[Path] = []
    for root in candidates:
        dll_dirs.append(root)
        pyside_root = root / "PySide6"
        dll_dirs.append(pyside_root)
        dll_dirs.append(root / "shiboken6")
        if pyside_root.is_dir():
            pyside_roots.append(pyside_root)

    seen: set[str] = set()
    for folder in dll_dirs:
        if not folder.is_dir():
            continue
        normalized = str(folder.resolve())
        if normalized.lower() in seen:
            continue
        seen.add(normalized.lower())
        try:
            os.add_dll_directory(normalized)
        except (FileNotFoundError, OSError):
            continue
        os.environ["PATH"] = normalized + os.pathsep + os.environ.get("PATH", "")

    for pyside_root in pyside_roots:
        plugins = pyside_root / "plugins"
        platforms = plugins / "platforms"
        if platforms.is_dir():
            os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", str(platforms.resolve()))
        if plugins.is_dir():
            os.environ.setdefault("QT_PLUGIN_PATH", str(plugins.resolve()))

    _CONFIGURED = True
