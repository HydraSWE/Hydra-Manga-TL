"""Local translation engines for Hydra.

Engines translate a *page dialogue payload* into a structured JSON result.
The rest of Hydra should not depend on model-specific code.
"""

from .base import PageDialogue, PageTranslation, TranslationEngine
from .model_manager import TranslationEngineManager

__all__ = [
    "PageDialogue",
    "PageTranslation",
    "TranslationEngine",
    "TranslationEngineManager",
]

