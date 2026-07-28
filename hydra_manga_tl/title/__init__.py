"""Hydra Style-Preserved Title Replacement System (HSTR)."""

from .compositor import composite_title
from .background_plate import TitleBackgroundPlateResult, apply_title_background_plates
from .detector import detect_title_objects
from .mask_extractor import TitleGlyphMaskResult, extract_title_glyph_mask
from .models import RenderedTitleLayer, TitleComposition, TitleLayer, TitleObject, TitleRenderSettings
from hydra_manga_tl.title.renderer import render_title, render_title_composition
from .style_extractor import extract_title_style
from .style_profile import (
    FillProfile,
    GlowProfile,
    GradientProfile,
    OutlineProfile,
    ShadowProfile,
    StrokeProfile,
    TitleStyleProfile,
    TypographyProfile,
)
from .utils import get_cached_title_profile, save_title_profile, title_fingerprint

__all__ = [
    "FillProfile",
    "GlowProfile",
    "GradientProfile",
    "OutlineProfile",
    "RenderedTitleLayer",
    "ShadowProfile",
    "StrokeProfile",
    "TitleBackgroundPlateResult",
    "TitleGlyphMaskResult",
    "TitleComposition",
    "TitleLayer",
    "TitleObject",
    "TitleRenderSettings",
    "TitleStyleProfile",
    "TypographyProfile",
    "apply_title_background_plates",
    "composite_title",
    "detect_title_objects",
    "extract_title_style",
    "extract_title_glyph_mask",
    "get_cached_title_profile",
    "render_title",
    "render_title_composition",
    "save_title_profile",
    "title_fingerprint",
]
