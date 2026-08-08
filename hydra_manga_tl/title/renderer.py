"""Layered HSTR title renderer."""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from hydra_manga_tl.core.fonts import default_font_file
from .models import RenderedTitleLayer, TitleComposition, TitleObject
from .style_profile import Color, TitleStyleProfile

DEFAULT_FONT = default_font_file(bold=True)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    if DEFAULT_FONT.is_file():
        try:
            return ImageFont.truetype(str(DEFAULT_FONT), size, layout_engine=ImageFont.Layout.BASIC)
        except AttributeError:
            return ImageFont.truetype(str(DEFAULT_FONT), size)
    return ImageFont.load_default()


def _text_box(draw: ImageDraw.ImageDraw, text: str, font, stroke_width: int = 0) -> tuple[int, int, int, int]:
    """Return stable approximate text bounds for title fitting.

    Pillow/FreeType can access-violate on Windows while measuring some long
    decorative title strings. The title renderer only needs relative bounds for
    fitting and centering, so use a deterministic approximation and leave actual
    glyph drawing to Pillow.
    """
    del draw
    size = int(getattr(font, "size", 12) or 12)
    width_units = 0.0
    for char in str(text):
        if char.isspace():
            width_units += 0.34
        elif char in "MW@#%&":
            width_units += 0.92
        elif char in "ilI.,'`|!":
            width_units += 0.32
        elif ord(char) > 127:
            width_units += 0.95
        else:
            width_units += 0.62
    width = int(round(width_units * size)) + stroke_width * 2
    height = int(round(size * 1.16)) + stroke_width * 2
    return (-stroke_width, -stroke_width, width, height)


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


def _profile_stroke(profile: TitleStyleProfile) -> tuple[Color | None, int]:
    if not profile.stroke:
        return None, 0
    width = int(round(profile.stroke.width or 0))
    return profile.stroke.color, max(0, width)


def _clamp_effects(
    profile: TitleStyleProfile,
    font_size: int,
    box_size: tuple[int, int],
    outline_width: int,
    stroke_width: int,
) -> tuple[int, int, float | None, tuple[int, int] | None]:
    max_outline = max(1, min(4, font_size // 10))
    max_stroke = max(0, min(3, font_size // 14))
    safe_outline = min(outline_width, max_outline)
    safe_stroke = min(stroke_width, max_stroke)
    glow_radius = None
    if profile.glow and profile.glow.radius:
        glow_radius = min(float(profile.glow.radius), max(1.0, font_size / 18.0), 3.0)
    shadow_offset = None
    if profile.shadow and profile.shadow.offset:
        limit_x = max(2, box_size[0] // 18)
        limit_y = max(2, box_size[1] // 18)
        shadow_offset = (
            max(-limit_x, min(limit_x, int(profile.shadow.offset[0]))),
            max(-limit_y, min(limit_y, int(profile.shadow.offset[1]))),
        )
    return safe_outline, safe_stroke, glow_radius, shadow_offset


def _opacity(value: float | None) -> int:
    return max(0, min(255, int(255 * (1.0 if value is None else value))))


def _with_opacity(color: Color, opacity: float | None) -> tuple[int, int, int, int]:
    return (*color, _opacity(opacity))


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
        draw.text((x, y), line, font=font, fill=_with_opacity(fill, profile.opacity), stroke_width=stroke_width, stroke_fill=stroke_fill)
        positions.append([x1 + int(x), y1 + int(y)])
        y += line_height
    return positions


def _draw_text_mask(
    size: tuple[int, int],
    title: TitleObject,
    lines: list[str],
    font_size: int,
    line_height: int,
    stroke_width: int = 0,
) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    font = _font(font_size)
    width = size[0]
    height = size[1]
    total_h = line_height * len(lines)
    y = max(0, (height - total_h) // 2)
    for line in lines:
        bounds = _text_box(draw, line, font, stroke_width)
        line_w = bounds[2] - bounds[0]
        if title.render_settings.alignment == "left":
            x = -bounds[0]
        elif title.render_settings.alignment == "right":
            x = width - line_w - bounds[0]
        else:
            x = max(0, (width - line_w) // 2) - bounds[0]
        draw.text((x, y), line, font=font, fill=255, stroke_width=stroke_width, stroke_fill=255)
        y += line_height
    return mask


def _gradient_fill(size: tuple[int, int], profile: TitleStyleProfile, fallback: Color) -> Image.Image:
    colors = profile.gradient.colors if profile.gradient else []
    if not profile.gradient or profile.gradient.kind != "linear" or len(colors) < 2:
        return Image.new("RGBA", size, _with_opacity(fallback, profile.opacity))
    first, second = colors[0], colors[-1]
    width, height = size
    angle = float(profile.gradient.angle or 90.0)
    vertical = abs(angle) % 180 >= 45 and abs(angle) % 180 <= 135
    gradient = Image.new("RGBA", size, (0, 0, 0, 0))
    pixels = gradient.load()
    span = max(1, height - 1 if vertical else width - 1)
    for y in range(height):
        for x in range(width):
            t = (y if vertical else x) / span
            color = tuple(int(first[index] + (second[index] - first[index]) * t) for index in range(3))
            pixels[x, y] = (*color, _opacity(profile.opacity))
    return gradient


def render_title(base_image: Image.Image, title: TitleObject, profile: TitleStyleProfile) -> RenderedTitleLayer:
    x1, y1, x2, y2 = title.box
    width = max(1, x2 - x1)
    height = max(1, y2 - y1)
    fill = _profile_fill(profile)
    outline_fill, outline_width = _profile_outline(profile)
    stroke_fill, stroke_width = _profile_stroke(profile)
    lines, font_size, line_height, overflow = _fit_text(
        title.translated_text,
        title.box,
        title.render_settings.max_font_size,
        title.render_settings.min_font_size,
        profile.typography.tracking if profile.typography else None,
        outline_width,
    )
    outline_width, stroke_width, glow_radius, shadow_offset = _clamp_effects(
        profile,
        font_size,
        (width, height),
        outline_width,
        stroke_width,
    )
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    positions: list[list[int]] = []

    if profile.glow and profile.glow.color and glow_radius:
        glow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        glow_profile = TitleStyleProfile.from_dict({**profile.to_dict(), "opacity": profile.glow.opacity})
        _draw_text_layer(glow_layer, title, glow_profile, lines, font_size, line_height, fill=profile.glow.color, stroke_width=max(1, outline_width + stroke_width))
        layer.alpha_composite(glow_layer.filter(ImageFilter.GaussianBlur(radius=glow_radius)))

    if profile.shadow and profile.shadow.color and shadow_offset:
        shadow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        shadow_profile = TitleStyleProfile.from_dict({**profile.to_dict(), "opacity": profile.shadow.opacity})
        _draw_text_layer(
            shadow_layer,
            title,
            shadow_profile,
            lines,
            font_size,
            line_height,
            fill=profile.shadow.color,
            stroke_width=max(0, outline_width + stroke_width),
            offset=shadow_offset,
        )
        if profile.shadow.blur:
            shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(radius=float(profile.shadow.blur)))
        layer.alpha_composite(shadow_layer)

    if stroke_fill and stroke_width:
        stroke_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        _draw_text_layer(stroke_layer, title, profile, lines, font_size, line_height, fill=stroke_fill, stroke_width=stroke_width + outline_width)
        layer.alpha_composite(stroke_layer)

    if outline_fill and outline_width:
        outline_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        _draw_text_layer(outline_layer, title, profile, lines, font_size, line_height, fill=outline_fill, stroke_width=outline_width)
        layer.alpha_composite(outline_layer)

    fill_layer = _gradient_fill((width, height), profile, fill)
    fill_mask = _draw_text_mask((width, height), title, lines, font_size, line_height)
    fill_layer.putalpha(fill_mask.point(lambda value: min(value, _opacity(profile.opacity))))
    positions = _draw_text_layer(Image.new("RGBA", (width, height), (0, 0, 0, 0)), title, profile, lines, font_size, line_height, fill=fill)
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


def render_title_composition(base_image: Image.Image, composition: TitleComposition) -> tuple[Image.Image, list[dict]]:
    output = base_image
    reports: list[dict] = []
    for layer in sorted(composition.layers, key=lambda item: item.hierarchy_rank, reverse=True):
        if not str(layer.translated_text).strip():
            continue
        title = layer.to_title_object(composition.id)
        rendered = render_title(output, title, layer.style_profile)
        x1, y1, _, _ = rendered.box
        composited = output.convert("RGBA")
        composited.alpha_composite(rendered.image.convert("RGBA"), (int(x1), int(y1)))
        output = composited.convert(output.mode) if output.mode != "RGBA" else composited
        report = rendered.report()
        report["layer_id"] = layer.id
        report["role"] = layer.role
        report["hierarchy_rank"] = layer.hierarchy_rank
        reports.append(report)
    return output, reports
