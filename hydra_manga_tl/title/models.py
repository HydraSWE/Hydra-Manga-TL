"""HSTR title objects and render result models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .style_profile import TitleStyleProfile
from .utils import box_from_polygon


@dataclass
class TitleRenderSettings:
    max_font_size: int = 74
    min_font_size: int = 10
    alignment: str = "center"
    vertical_alignment: str = "center"
    allow_rotation: bool = True
    overflow: str = "shrink"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "TitleRenderSettings":
        payload = payload or {}
        return cls(
            max_font_size=int(payload.get("max_font_size", payload.get("art_text_max_font", 74)) or 74),
            min_font_size=int(payload.get("min_font_size", 10) or 10),
            alignment=str(payload.get("alignment", "center")),
            vertical_alignment=str(payload.get("vertical_alignment", "center")),
            allow_rotation=bool(payload.get("allow_rotation", True)),
            overflow=str(payload.get("overflow", "shrink")),
        )


@dataclass
class TitleObject:
    id: str
    polygon: list[list[int]]
    original_text: str
    translated_text: str
    style_profile: TitleStyleProfile = field(default_factory=TitleStyleProfile)
    render_settings: TitleRenderSettings = field(default_factory=TitleRenderSettings)
    history: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_group(cls, group: dict[str, Any]) -> "TitleObject | None":
        if not isinstance(group, dict):
            return None
        polygon = group.get("polygon") or group.get("manual_rect")
        if isinstance(polygon, list) and len(polygon) == 4 and all(isinstance(value, (int, float)) for value in polygon):
            x1, y1, x2, y2 = [int(value) for value in polygon]
            polygon = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
        if not polygon:
            return None
        renderable_type = str(group.get("renderable_type") or group.get("type") or group.get("bubble_type") or "").lower()
        render_mode = str(group.get("render_mode") or "").lower()
        is_title_like = (
            renderable_type in {"title", "decorative_text", "decorative", "sfx", "sign", "credit"}
            or render_mode == "art_text"
            or group.get("art_text") is True
        )
        if not is_title_like:
            return None
        settings_payload = group.get("render_settings") if isinstance(group.get("render_settings"), dict) else {}
        metadata = {
            "group_index": group.get("index"),
            "source_polygons": group.get("source_polygons") or [polygon],
            "source_member_texts": group.get("source_member_texts") or [],
            "renderable_type": renderable_type or "title",
            "render_mode": render_mode or group.get("render_mode"),
        }
        metadata.update(dict(group.get("title_metadata", {})) if isinstance(group.get("title_metadata"), dict) else {})
        return cls(
            id=str(group.get("title_id") or group.get("id") or group.get("index") or "title"),
            polygon=[[int(x), int(y)] for x, y in polygon],
            original_text=str(group.get("original_text") or group.get("text") or ""),
            translated_text=str(group.get("translated_text") or ""),
            style_profile=TitleStyleProfile.from_dict(group.get("style_profile") if isinstance(group.get("style_profile"), dict) else None),
            render_settings=TitleRenderSettings.from_dict({
                **settings_payload,
                "art_text_max_font": group.get("art_text_max_font", settings_payload.get("max_font_size", 74)),
                "alignment": group.get("alignment", settings_payload.get("alignment", "center")),
            }),
            history=list(group.get("history", [])) if isinstance(group.get("history"), list) else [],
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "polygon": self.polygon,
            "original_text": self.original_text,
            "translated_text": self.translated_text,
            "style_profile": self.style_profile.to_dict(),
            "render_settings": self.render_settings.to_dict(),
            "history": self.history,
            "metadata": self.metadata,
        }

    def to_group_patch(self) -> dict[str, Any]:
        return {
            "title_id": self.id,
            "renderable_type": self.metadata.get("renderable_type", "title"),
            "render_mode": self.metadata.get("render_mode") or "art_text",
            "style_profile": self.style_profile.to_dict(),
            "render_settings": self.render_settings.to_dict(),
            "title_metadata": dict(self.metadata),
        }

    @property
    def box(self) -> list[int]:
        return box_from_polygon(self.polygon)


@dataclass
class RenderedTitleLayer:
    image: Any
    box: list[int]
    text: str
    lines: list[str]
    font_size: int
    overflow: bool
    positions: list[list[int]]
    profile: TitleStyleProfile

    def report(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "lines": self.lines,
            "font_size": self.font_size,
            "box": self.box,
            "overflow": self.overflow,
            "positions": self.positions,
            "style_profile": self.profile.to_dict(strip_none=True),
        }
