"""Optional manga-specialized recognition layered over Paddle geometry."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path
import re
from typing import Any

from PIL import Image

from hydra_manga_tl.ocr.core import clean_ocr_text


MODEL_ID = "kha-white/manga-ocr-base"
DECORATIVE_SYMBOLS = frozenset("♪♫♬♩♡♥❤★☆◇◆■□●○◎")


@dataclass(frozen=True)
class HybridRecognition:
    text: str
    recognizer: str
    paddle_candidate: str
    manga_candidate: str
    agreement: float
    review_reasons: list[str]
    fallback_reason: str = ""
    inline_symbols: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def split_inline_decorative_symbols(text: str) -> tuple[str, list[str]]:
    symbols = [char for char in str(text) if char in DECORATIVE_SYMBOLS]
    cleaned = "".join(char for char in str(text) if char not in DECORATIVE_SYMBOLS)
    return cleaned.strip(), symbols


def _comparison_text(text: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(text), flags=re.UNICODE)


def _valid_japanese(text: str) -> bool:
    compact = _comparison_text(text)
    return bool(compact) and any(
        "\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff"
        for char in compact
    )


def choose_japanese_recognition(manga_text: str, paddle_text: str) -> HybridRecognition:
    manga_clean, symbols = split_inline_decorative_symbols(manga_text)
    paddle_clean, _ = split_inline_decorative_symbols(paddle_text)
    manga_clean = clean_ocr_text(manga_clean)
    paddle_clean = clean_ocr_text(paddle_clean)
    if not manga_clean:
        return HybridRecognition(
            paddle_clean, "paddle", paddle_clean, manga_clean, 0.0, [],
            "empty_result", inline_symbols=[],
        )
    if not _valid_japanese(manga_clean):
        return HybridRecognition(
            paddle_clean, "paddle", paddle_clean, manga_clean, 0.0,
            ["ocr:manga_invalid_japanese_script"],
            "invalid_japanese_script", inline_symbols=[],
        )
    agreement = SequenceMatcher(
        None, _comparison_text(manga_clean), _comparison_text(paddle_clean)
    ).ratio()
    review = [] if agreement >= 0.86 else ["ocr:manga_paddle_disagreement"]
    return HybridRecognition(
        manga_clean, "manga-ocr", paddle_clean, manga_clean, agreement, review,
        inline_symbols=symbols,
    )


class MangaOCRRuntime:
    """Lazy CPU-only model owner; one instance lives inside the OCR worker."""

    def __init__(self, model_cache: Path) -> None:
        self.model_cache = Path(model_cache)
        self._engine = None
        self._load_error = ""

    def _load(self):
        if self._engine is not None:
            return self._engine
        if self._load_error:
            raise RuntimeError(self._load_error)
        try:
            from huggingface_hub import snapshot_download
            from manga_ocr import MangaOcr

            self.model_cache.mkdir(parents=True, exist_ok=True)
            snapshot = snapshot_download(repo_id=MODEL_ID, cache_dir=self.model_cache)
            self._engine = MangaOcr(snapshot, force_cpu=True)
            return self._engine
        except Exception as error:
            self._load_error = f"{type(error).__name__}: {error}"
            raise

    def recognize_groups(self, image_path: Path, groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
        try:
            engine = self._load()
        except Exception as error:
            return [
                choose_japanese_recognition("", str(group.get("paddle_text", ""))).to_dict()
                | {"fallback_reason": f"model_unavailable:{type(error).__name__}"}
                for group in groups
            ]

        with Image.open(image_path) as opened:
            image = opened.convert("RGB")
        results: list[dict[str, Any]] = []
        for group in groups:
            x1, y1, x2, y2 = [int(value) for value in group["bbox"]]
            pad_x = max(6, round((x2 - x1) * 0.08))
            pad_y = max(6, round((y2 - y1) * 0.08))
            crop = image.crop((
                max(0, x1 - pad_x), max(0, y1 - pad_y),
                min(image.width, x2 + pad_x), min(image.height, y2 + pad_y),
            ))
            try:
                result = choose_japanese_recognition(
                    str(engine(crop)), str(group.get("paddle_text", ""))
                ).to_dict()
            except Exception as error:
                result = choose_japanese_recognition("", str(group.get("paddle_text", ""))).to_dict()
                result["fallback_reason"] = f"inference_failed:{type(error).__name__}"
            results.append(result)
        return results
