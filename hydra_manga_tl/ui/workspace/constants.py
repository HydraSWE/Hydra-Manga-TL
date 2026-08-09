"""Workspace UI constants."""

from __future__ import annotations

TRANSLATE_ELIGIBLE_STATUSES = {"pending", "queued", "failed", "cancelled"}

PROGRESS_RANGES = {
    "preprocessing": (0.0, 5.0),
    "analyzing": (0.0, 5.0),
    "OCR": (5.0, 30.0),
    "ocr": (5.0, 30.0),
    "translating": (30.0, 50.0),
    "rendering": (50.0, 90.0),
    "reconstructing": (50.0, 98.0),
    "review": (90.0, 98.0),
}
