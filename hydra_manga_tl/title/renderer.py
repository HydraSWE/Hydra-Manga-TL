"""Layered HSTR title renderer."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from .models import RenderedTitleLayer, TitleObject
from .style_profile import Color, TitleStyleProfile

DEFAULT_FONT = Path(r"C:\Windows\Fonts\arialbd.ttf")


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if DEFAULT_FONT.is_file():
        return ImageFont.truetype(str(DEFAULT_FONT), size)
    return ImageFont.load_default()


def _text_box(draw: ImageDraw.ImageDraw, text: str, font, stroke_width: int = 0) -> tuple[int, int, int, int]:
    return draw.textbbox((0, 0), text, font=font, stroke_width=stroke_width)


def _wrap_words(text: str, draw: ImageDraw.ImageDraw, font, width: int, stroke_width: int) -> list[str]:
    words = [word for word in str(text).replace("\n", " ").split(" ") if word]
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        bounds = _text_box(draw, candidate, font, stroke_width)
        if bounds[2] - bounds[0] <= width:
            current = candidate
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _fit_text(text: str, box: list[int], maximum: int, minimum: int, tracking: float | None, stroke_width: int) -> tuple[list[str], int, int, bool]:
    width = max(1, box[2] - box[0])
    height = max(1, box[3] - box[1])
    pad = max(2, int(min(width, height) * 0.06))
    usable_w = max(1, width - pad * 2)
    usable_h = max(1, height - pad * 2)
    scratch = Image.new("L", (width, height))
    draw = ImageDraw.Draw(scratch)
    for size in range(maximum, minimum - 1, -1):
        font = _font(size)
        current_stroke = max(stroke_width, int(size // 11)) if stroke_width else 0
        lines = _wrap_words(text, draw, font, usable_w, current_stroke)
        if not lines:
            continue
        sample = _text_box(draw, "Ag", font, current_stroke)
        line_h = max(1, sample[3] - sample[1] + max(2, size // 7))
        max_w = max((_text_box(draw, line, font, current_stroke)[2] for line in lines), default=0)
        extra_tracking = int(max(0.0, tracking or 0.0) * max(0, max(len(line) for line in lines) - 1))
        if max_w + extra_tracking <= usable_w and line_h * len(lines) <= usable_h:
            return lines, size, line_h, False
    return [text], minimum, max(1, minimum + 2), True


def _profile_fill(profile: TitleStyleProfile) -> Color:
    if profile.fill:
        return profile.fill.dominant_color or profile.fill.average_color or (15, 15, 15)
    return (15, 15, 15)


def _profile_outline(profile: TitleStyleProfile) -> tuple[Color | None, int]:
    if not profile.outline:
        return None, 0
    width = int(round(profile.outline.width or 0))
    return profile.outline.color, max(0, width)


def _opacity(value: float | None) -> int:
    return max(0, min(255, int(255 * (1.0 if value is None else value))))


def _draw_text_layer(
    layer: Image.Image,
    title: TitleObject,
    profile: TitleStyleProfile,
    lines: list[str],
    font_size: int,
    line_height: int,
    *,
    fill: Color,
    stroke_fill: Color | None = None,
    stroke_width: int = 0,
    offset: tuple[int, int] = (0, 0),
) -> list[list[int]]:
    draw = ImageDraw.Draw(layer)
    font = _font(font_size)
    x1, y1, x2, y2 = title.box
    width = max(1, x2 - x1)
    height = max(1, y2 - y1)
    total_h = line_height * len(lines)
    y = max(0, (height - total_h) // 2) + offset[1]
    positions: list[list[int]] = []
    for line in lines:
        bounds = _text_box(draw, line, font, stroke_width)
        line_w = bounds[2] - bounds[0]
        if title.render_settings.alignment == "left":
            x = -bounds[0]
        elif title.render_settings.alignment == "right":
            x = width - line_w - bounds[0]
        else:
            x = max(0, (width - line_w) // 2) - bounds[0]
        x += offset[0]
        draw.text((x, y), line, font=font, fill=(*fill, _opacity(profile.opacity)), stroke_width=stroke_width, stroke_fill=stroke_fill)
        positions.append([x1 + int(x), y1 + int(y)])
        y += line_height
    return positions


def render_title(base_image: Image.Image, title: TitleObject, profile: TitleStyleProfile) -> RenderedTitleLayer:
    x1, y1, x2, y2 = title.box
    width = max(1, x2 - x1)
    height = max(1, y2 - y1)
    fill = _profile_fill(profile)
    outline_fill, outline_width = _profile_outline(profile)
    lines, font_size, line_height, overflow = _fit_text(
        title.translated_text,
        title.box,
        title.render_settings.max_font_size,
        title.render_settings.min_font_size,
        profile.typography.tracking if profile.typography else None,
        outline_width,
    )
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    positions: list[list[int]] = []

    if profile.glow and profile.glow.color and profile.glow.radius:
        glow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        _draw_text_layer(glow_layer, title, profile, lines, font_size, line_height, fill=profile.glow.color, stroke_width=max(1, outline_width))
        layer.alpha_composite(glow_layer.filter(ImageFilter.GaussianBlur(radius=float(profile.glow.radius))))

    if profile.shadow and profile.shadow.color and profile.shadow.offset:
        shadow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        _draw_text_layer(
            shadow_layer,
            title,
            profile,
            lines,
            font_size,
            line_height,
            fill=profile.shadow.color,
            stroke_width=max(0, outline_width),
            offset=profile.shadow.offset,
        )
        if profile.shadow.blur:
            shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=float(profile.shadow.blur)))
        layer.alpha_composite(shadow_layer)

    if outline_fill and outline_width:
        outline_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        _draw_text_layer(outline_layer, title, profile, lines, font_size, line_height, fill=outline_fill, stroke_width=outline_width)
        layer.alpha_composite(outline_layer)

    fill_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    positions = _draw_text_layer(
        fill_layer,
        title,
        profile,
        lines,
        font_size,
        line_height,
        fill=fill,
        stroke_fill=outline_fill,
        stroke_width=outline_width,
    )
    layer.alpha_composite(fill_layer)

    if title.render_settings.allow_rotation and profile.rotation:
        angle = float(profile.rotation)
        if abs(angle) >= 1.0:
            layer = layer.rotate(angle, expand=True, resample=Image.Resampling.BICUBIC)

    return RenderedTitleLayer(
        image=layer,
        box=[x1, y1, x1 + layer.width, y1 + layer.height],
        text=title.translated_text,
        lines=lines,
        font_size=font_size,
        overflow=overflow,
        positions=positions,
        profile=profile,
    )
