"""Compositing helpers for HSTR rendered title layers."""

from __future__ import annotations

from PIL import Image

from .models import RenderedTitleLayer, TitleRenderSettings


def composite_title(
    base_image: Image.Image,
    rendered_layer: RenderedTitleLayer,
    settings: TitleRenderSettings | None = None,
) -> Image.Image:
    output = base_image.convert("RGBA")
    x1, y1, _, _ = rendered_layer.box
    output.alpha_composite(rendered_layer.image.convert("RGBA"), (int(x1), int(y1)))
    if base_image.mode != "RGBA":
        return output.convert(base_image.mode)
    return output
