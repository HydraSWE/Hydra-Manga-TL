"""Title candidate adapters for current OCR/group payloads."""

from __future__ import annotations

from typing import Any

from .models import TitleObject


def _looks_like_title_group(group: dict[str, Any], image_size: tuple[int, int] | None = None) -> bool:
    group_type = str(group.get("type") or group.get("bubble_type") or "").strip().lower()
    if group.get("art_text") is True or str(group.get("render_mode") or "").lower() == "art_text":
        return True
    if group_type in {"title", "decorative", "decorative_text", "sign", "sfx", "credit"}:
        return True
    if image_size is None:
        return False
    polygon = group.get("polygon") or []
    if not polygon:
        return False
    xs = [int(point[0]) for point in polygon]
    ys = [int(point[1]) for point in polygon]
    width, height = image_size
    box_w = max(1, max(xs) - min(xs))
    box_h = max(1, max(ys) - min(ys))
    vertical = box_h > box_w * 1.65
    near_edge = min(xs) > width * 0.55 or max(xs) > width * 0.72
    tall = box_h > height * 0.24
    return bool(vertical and near_edge and tall)


def detect_title_objects(groups: list[dict[str, Any]], image_size: tuple[int, int] | None = None) -> list[TitleObject]:
    titles: list[TitleObject] = []
    for group in groups:
        if not _looks_like_title_group(group, image_size):
            continue
        title = TitleObject.from_group({
            **group,
            "renderable_type": group.get("renderable_type") or group.get("type") or "title",
            "render_mode": group.get("render_mode") or "art_text",
        })
        if title is not None:
            titles.append(title)
    return titles
