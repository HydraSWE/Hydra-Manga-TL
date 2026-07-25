"""Hydra Style-Preserved Title Replacement System (HSTR)."""

from .compositor import composite_title
from .detector import detect_title_objects
from .models import RenderedTitleLayer, TitleObject, TitleRenderSettings
from .renderer import render_title
from .style_extractor import extract_title_style
from .style_profile import (
    FillProfile,
    GlowProfile,
    GradientProfile,
    OutlineProfile,
    ShadowProfile,
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
    "TitleObject",
    "TitleRenderSettings",
    "TitleStyleProfile",
    "TypographyProfile",
    "composite_title",
    "detect_title_objects",
    "extract_title_style",
    "get_cached_title_profile",
    "render_title",
    "save_title_profile",
    "title_fingerprint",
]
