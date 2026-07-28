"""HSTR title objects and render result models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .style_profile import FillProfile, TitleStyleProfile
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
class TitleLayer:
    id: str
    polygon: list[list[int]]
    original_text: str
    translated_text: str
    role: str = "main"
    hierarchy_rank: int = 0
    style_profile: TitleStyleProfile = field(default_factory=TitleStyleProfile)
    render_settings: TitleRenderSettings = field(default_factory=TitleRenderSettings)
    transform: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def box(self) -> list[int]:
        return box_from_polygon(self.polygon)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "TitleLayer | None":
        if not isinstance(payload, dict):
            return None
        polygon = payload.get("polygon")
        if not polygon:
            return None
        return cls(
            id=str(payload.get("id") or "layer"),
            polygon=[[int(x), int(y)] for x, y in polygon],
            original_text=str(payload.get("original_text") or payload.get("text") or ""),
            translated_text=str(payload.get("translated_text") or ""),
            role=str(payload.get("role") or "main"),
            hierarchy_rank=int(payload.get("hierarchy_rank", 0) or 0),
            style_profile=TitleStyleProfile.from_dict(payload.get("style_profile") if isinstance(payload.get("style_profile"), dict) else None),
            render_settings=TitleRenderSettings.from_dict(payload.get("render_settings") if isinstance(payload.get("render_settings"), dict) else None),
            transform=dict(payload.get("transform", {})) if isinstance(payload.get("transform"), dict) else {},
            metadata=dict(payload.get("metadata", {})) if isinstance(payload.get("metadata"), dict) else {},
        )

    def to_title_object(self, composition_id: str) -> TitleObject:
        return TitleObject(
            id=f"{composition_id}:{self.id}",
            polygon=self.polygon,
            original_text=self.original_text,
            translated_text=self.translated_text,
            style_profile=self.style_profile,
            render_settings=self.render_settings,
            metadata={**self.metadata, "renderable_type": "title", "render_mode": "art_text", "role": self.role},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "polygon": self.polygon,
            "original_text": self.original_text,
            "translated_text": self.translated_text,
            "role": self.role,
            "hierarchy_rank": self.hierarchy_rank,
            "style_profile": self.style_profile.to_dict(),
            "render_settings": self.render_settings.to_dict(),
            "transform": self.transform,
            "metadata": self.metadata,
        }


@dataclass
class TitleComposition:
    id: str
    source_polygons: list[list[list[int]]]
    layers: list[TitleLayer] = field(default_factory=list)
    hierarchy: list[str] = field(default_factory=list)
    suggestions: list[dict[str, Any]] = field(default_factory=list)
    selected_layout: str = "source"
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "TitleComposition | None":
        if not isinstance(payload, dict):
            return None
        layers = [layer for layer in (TitleLayer.from_dict(item) for item in payload.get("layers", [])) if layer is not None]
        return cls(
            id=str(payload.get("id") or "title"),
            source_polygons=[
                [[int(x), int(y)] for x, y in polygon]
                for polygon in payload.get("source_polygons", [])
                if polygon
            ],
            layers=layers,
            hierarchy=[str(item) for item in payload.get("hierarchy", [])],
            suggestions=[dict(item) for item in payload.get("suggestions", []) if isinstance(item, dict)],
            selected_layout=str(payload.get("selected_layout") or "source"),
            metadata=dict(payload.get("metadata", {})) if isinstance(payload.get("metadata"), dict) else {},
        )

    @classmethod
    def from_group(cls, group: dict[str, Any], chunks: list[str], polygons: list[list[list[int]]]) -> "TitleComposition":
        existing = cls.from_dict(group.get("title_composition") if isinstance(group.get("title_composition"), dict) else None)
        if existing is not None and existing.layers:
            return existing
        source_texts = group.get("source_member_texts") or []
        source_text_colors = group.get("source_text_colors") or group.get("source_member_colors") or []
        style_profile = TitleStyleProfile.from_dict(group.get("style_profile") if isinstance(group.get("style_profile"), dict) else None)
        settings = TitleRenderSettings(
            max_font_size=int(group.get("art_text_max_font", 74) or 74),
            min_font_size=10,
            alignment=str(group.get("alignment", "center")),
        )
        layers: list[TitleLayer] = []
        ranked = sorted(
            enumerate(polygons),
            key=lambda item: _polygon_area(item[1]),
            reverse=True,
        )
        ranks = {index: rank for rank, (index, _) in enumerate(ranked)}
        explicit_layer_texts = group.get("title_layer_translations")
        use_ranked_single_title = (
            not isinstance(explicit_layer_texts, list)
            and not source_text_colors
            and len(polygons) > 1
            and len(str(group.get("translated_text") or "").split()) >= max(5, len(polygons) * 2)
        )
        for index, polygon in enumerate(polygons):
            text = str(source_texts[index]) if index < len(source_texts) else str(group.get("original_text") or "")
            rank = ranks.get(index, index)
            if isinstance(explicit_layer_texts, list):
                translated = str(explicit_layer_texts[index]).upper() if index < len(explicit_layer_texts) else ""
            elif use_ranked_single_title:
                translated = str(group.get("translated_text") or "").upper() if rank == 0 else ""
            else:
                translated = str(chunks[index]).upper() if index < len(chunks) else ""
            layer_style = TitleStyleProfile.from_dict(style_profile.to_dict())
            color = _color_tuple(source_text_colors[index]) if index < len(source_text_colors) else None
            if color is not None:
                layer_style.fill = FillProfile(dominant_color=color, average_color=color, colors=[color])
                layer_style.metadata = {**layer_style.metadata, "source_text_color": list(color)}
            layers.append(TitleLayer(
                id=f"layer-{index + 1}",
                polygon=[[int(x), int(y)] for x, y in polygon],
                original_text=text,
                translated_text=translated,
                role="main" if rank == 0 else ("subtitle" if rank == 1 else "detail"),
                hierarchy_rank=rank,
                style_profile=layer_style,
                render_settings=settings,
            ))
        hierarchy = [layer.id for layer in sorted(layers, key=lambda item: item.hierarchy_rank)]
        suggestions = layout_suggestions(layers)
        return cls(
            id=str(group.get("title_id") or group.get("index") or "art"),
            source_polygons=polygons,
            layers=layers,
            hierarchy=hierarchy,
            suggestions=suggestions,
            selected_layout=str(group.get("selected_title_layout") or "source"),
            metadata={"renderable_type": "title", "render_mode": "art_text", "schema": "hstr-composition-v2"},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "source_polygons": self.source_polygons,
            "layers": [layer.to_dict() for layer in self.layers],
            "hierarchy": self.hierarchy,
            "suggestions": self.suggestions,
            "selected_layout": self.selected_layout,
            "metadata": self.metadata,
        }


def _polygon_area(polygon: list[list[int]]) -> int:
    box = box_from_polygon(polygon)
    return max(1, box[2] - box[0]) * max(1, box[3] - box[1])


def _color_tuple(value: Any) -> tuple[int, int, int] | None:
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        try:
            return tuple(max(0, min(255, int(component))) for component in value[:3])
        except (TypeError, ValueError):
            return None
    return None


def layout_suggestions(layers: list[TitleLayer]) -> list[dict[str, Any]]:
    if not layers:
        return []
    ordered = sorted(layers, key=lambda item: item.hierarchy_rank)
    suggestions = [{"id": "source", "label": "Layout A", "layer_order": [layer.id for layer in ordered]}]
    suggestions.append({"id": "stacked", "label": "Layout B", "layer_order": [layer.id for layer in ordered], "line_policy": "balanced_stack"})
    suggestions.append({"id": "compact", "label": "Layout C", "layer_order": [layer.id for layer in ordered], "line_policy": "short_title_lines"})
    return suggestions


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
