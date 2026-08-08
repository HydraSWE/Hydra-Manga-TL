"""Shared Windows and bundled font discovery for rendering and startup."""

from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import re

from hydra_manga_tl.core.assets import asset_roots


FONT_FILENAMES: dict[str, tuple[str, ...]] = {
    "Arial": ("arial.ttf",),
    "Arial Bold": ("arialbd.ttf",),
    "Comic Sans MS": ("comic.ttf",),
    "Segoe UI": ("segoeui.ttf",),
    "Yu Gothic": ("YuGothM.ttc", "YuGothR.ttc"),
    "MS Gothic": ("msgothic.ttc",),
    "Meiryo": ("meiryo.ttc",),
}
FONT_EXTENSIONS = {".ttf", ".otf", ".ttc"}


def _font_roots() -> tuple[Path, ...]:
    roots = [
        *(root / "assets" / "fonts" for root in asset_roots()),
        Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts",
    ]
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root).casefold()
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return tuple(unique)


def _normalized_font_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value).casefold())


@lru_cache(maxsize=64)
def find_font_file(font_family: str) -> Path | None:
    filenames = FONT_FILENAMES.get(str(font_family), ())
    roots = _font_roots()
    for root in roots:
        for filename in filenames:
            candidate = root / filename
            if candidate.is_file():
                return candidate

    requested = _normalized_font_name(font_family)
    if not requested:
        return None
    for root in roots:
        if not root.is_dir():
            continue
        try:
            candidates = tuple(root.iterdir())
        except OSError:
            continue
        for candidate in candidates:
            if (
                candidate.is_file()
                and candidate.suffix.casefold() in FONT_EXTENSIONS
                and _normalized_font_name(candidate.stem).startswith(requested)
            ):
                return candidate
    return None


def default_font_file(*, bold: bool = False) -> Path:
    preferred = (
        ("Arial Bold", "Segoe UI", "Arial", "Yu Gothic")
        if bold
        else ("Arial", "Segoe UI", "Yu Gothic", "MS Gothic", "Meiryo")
    )
    for family in preferred:
        candidate = find_font_file(family)
        if candidate is not None:
            return candidate
    raise FileNotFoundError(
        "No supported render font was found in bundled, system, or user font folders."
    )


def resolve_font_file(
    font_family: str,
    *,
    fallback: Path | None = None,
    bold_fallback: bool = False,
) -> Path:
    candidate = find_font_file(font_family)
    if candidate is not None:
        return candidate
    if fallback is not None and Path(fallback).is_file():
        return Path(fallback)
    return default_font_file(bold=bold_fallback)
