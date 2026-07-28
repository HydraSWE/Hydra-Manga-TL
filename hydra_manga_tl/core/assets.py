"""Packaged and source-tree asset discovery."""

from __future__ import annotations

import sys
from pathlib import Path


def asset_roots() -> list[Path]:
    package_root = Path(__file__).resolve().parent.parent
    roots = [
        package_root,
        package_root.parent,
        Path(getattr(sys, "_MEIPASS", "") or ".").resolve(),
        Path(sys.executable).resolve().parent,
        Path(sys.executable).resolve().parent / "_internal",
    ]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def find_asset(*parts: str) -> Path | None:
    for root in asset_roots():
        candidate = root / "assets" / Path(*parts)
        if candidate.is_file():
            return candidate
    return None
