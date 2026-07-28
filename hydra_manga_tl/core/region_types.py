"""Region type normalization and renderer routing helpers."""

from __future__ import annotations

from typing import Any


DIALOGUE_TYPES = {"dialogue", "speech", "narration"}
TITLE_LIKE_TYPES = {"title", "sfx", "sign", "credit"}
REGION_TYPES = {"dialogue", *TITLE_LIKE_TYPES}


def normalize_region_type(value: Any) -> str:
    """Return the canonical region type used by translation and render routing."""
    kind = str(value or "").strip().lower().replace("-", "_")
    if kind in DIALOGUE_TYPES:
        return "dialogue"
    if kind in TITLE_LIKE_TYPES:
        return kind
    return "dialogue"


def group_region_type(group: dict[str, Any]) -> str:
    """Resolve a group payload to the canonical region type."""
    return normalize_region_type(
        group.get("bubble_type")
        or group.get("type")
        or group.get("renderable_type")
    )


def is_title_like_region(group_or_type: dict[str, Any] | str) -> bool:
    """Return True when the region should use the title-like renderer."""
    if isinstance(group_or_type, dict):
        return group_region_type(group_or_type) in TITLE_LIKE_TYPES
    return normalize_region_type(group_or_type) in TITLE_LIKE_TYPES

