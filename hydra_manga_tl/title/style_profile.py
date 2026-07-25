"""Serializable title style profiles for HSTR."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any

Color = tuple[int, int, int]


def _color(value: Any) -> Color | None:
    if value is None:
        return None
    if isinstance(value, str) and value.startswith("#") and len(value) == 7:
        return tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))
    if isinstance(value, (list, tuple)) and len(value) >= 3:
        return tuple(max(0, min(255, int(component))) for component in value[:3])
    return None


def _strip_none(value: Any) -> Any:
    if is_dataclass(value):
        return _strip_none(asdict(value))
    if isinstance(value, dict):
        return {key: _strip_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_strip_none(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_none(item) for item in value]
    return value


@dataclass
class FillProfile:
    dominant_color: Color | None = None
    average_color: Color | None = None
    colors: list[Color] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "FillProfile":
        payload = payload or {}
        return cls(
            dominant_color=_color(payload.get("dominant_color")),
            average_color=_color(payload.get("average_color")),
            colors=[color for color in (_color(item) for item in payload.get("colors", [])) if color],
        )


@dataclass
class OutlineProfile:
    color: Color | None = None
    width: float | None = None
    colors: list[Color] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "OutlineProfile":
        payload = payload or {}
        width = payload.get("width")
        return cls(
            color=_color(payload.get("color")),
            width=float(width) if width is not None else None,
            colors=[color for color in (_color(item) for item in payload.get("colors", [])) if color],
        )


@dataclass
class ShadowProfile:
    color: Color | None = None
    offset: tuple[int, int] | None = None
    blur: float | None = None
    opacity: float | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "ShadowProfile":
        payload = payload or {}
        offset = payload.get("offset")
        return cls(
            color=_color(payload.get("color")),
            offset=tuple(int(value) for value in offset[:2]) if isinstance(offset, (list, tuple)) and len(offset) >= 2 else None,
            blur=float(payload["blur"]) if payload.get("blur") is not None else None,
            opacity=float(payload["opacity"]) if payload.get("opacity") is not None else None,
        )


@dataclass
class GlowProfile:
    color: Color | None = None
    radius: float | None = None
    opacity: float | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "GlowProfile":
        payload = payload or {}
        return cls(
            color=_color(payload.get("color")),
            radius=float(payload["radius"]) if payload.get("radius") is not None else None,
            opacity=float(payload["opacity"]) if payload.get("opacity") is not None else None,
        )


@dataclass
class GradientProfile:
    kind: str | None = None
    colors: list[Color] = field(default_factory=list)
    angle: float | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "GradientProfile":
        payload = payload or {}
        return cls(
            kind=str(payload["kind"]) if payload.get("kind") is not None else None,
            colors=[color for color in (_color(item) for item in payload.get("colors", [])) if color],
            angle=float(payload["angle"]) if payload.get("angle") is not None else None,
        )


@dataclass
class TypographyProfile:
    family_hint: str | None = None
    weight: str | None = None
    tracking: float | None = None
    categories: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "TypographyProfile":
        payload = payload or {}
        return cls(
            family_hint=str(payload["family_hint"]) if payload.get("family_hint") is not None else None,
            weight=str(payload["weight"]) if payload.get("weight") is not None else None,
            tracking=float(payload["tracking"]) if payload.get("tracking") is not None else None,
            categories=[str(item) for item in payload.get("categories", [])],
        )


@dataclass
class TitleStyleProfile:
    fill: FillProfile | None = None
    outline: OutlineProfile | None = None
    shadow: ShadowProfile | None = None
    glow: GlowProfile | None = None
    gradient: GradientProfile | None = None
    typography: TypographyProfile | None = None
    rotation: float | None = None
    opacity: float | None = None
    blend_mode: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, strip_none: bool = False) -> dict[str, Any]:
        payload = asdict(self)
        return _strip_none(payload) if strip_none else payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> "TitleStyleProfile":
        payload = payload or {}
        valid = {item.name for item in fields(cls)}
        safe = {key: value for key, value in payload.items() if key in valid}
        return cls(
            fill=FillProfile.from_dict(safe.get("fill")) if safe.get("fill") is not None else None,
            outline=OutlineProfile.from_dict(safe.get("outline")) if safe.get("outline") is not None else None,
            shadow=ShadowProfile.from_dict(safe.get("shadow")) if safe.get("shadow") is not None else None,
            glow=GlowProfile.from_dict(safe.get("glow")) if safe.get("glow") is not None else None,
            gradient=GradientProfile.from_dict(safe.get("gradient")) if safe.get("gradient") is not None else None,
            typography=TypographyProfile.from_dict(safe.get("typography")) if safe.get("typography") is not None else None,
            rotation=float(safe["rotation"]) if safe.get("rotation") is not None else None,
            opacity=float(safe["opacity"]) if safe.get("opacity") is not None else None,
            blend_mode=str(safe["blend_mode"]) if safe.get("blend_mode") is not None else None,
            metadata=dict(safe.get("metadata", {})),
        )
