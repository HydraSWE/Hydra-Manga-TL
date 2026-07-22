"""Versioned unified Manga TL project schema."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from .editor_project import RegionEdit


PROJECT_VERSION = 5
STAGES = {"queued", "partial", "analyzing", "ocr", "translating", "localizing", "reconstructing", "ready", "review", "failed", "cancelled"}


@dataclass
class ManualRegion:
    id: str
    rect: list[int]
    source_polygons: list[list[list[int]]]
    original_text: str
    translated_text: str
    ocr_confidence: float
    source_language: str
    direction: str
    status: str
    review_reasons: list[str] = field(default_factory=list)
    suppressed_auto_group_indices: list[int] = field(default_factory=list)
    source_member_texts: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"manual:{self.id}"


@dataclass
class ImageRecord:
    id: str
    source_path: str
    relative_path: str
    status: str = "queued"
    source_language: str = ""
    error: str = ""
    ocr_result: str = ""
    translation_result: str = ""
    rendered_image: str = ""
    preview_image: str = ""
    edits: dict[str, RegionEdit] = field(default_factory=dict)
    manual_regions: list[ManualRegion] = field(default_factory=list)
    suppressed_auto_group_indices: list[int] = field(default_factory=list)

    @property
    def source(self) -> Path:
        return Path(self.source_path)


@dataclass
class MangaProject:
    id: str
    name: str
    root: str
    version: int = PROJECT_VERSION
    target_language: str = "en"
    source_language: str = "auto"
    quality: str = "Balanced"
    literal_provider: str = "marian"
    localization_provider: str = "local"
    localization_model: str = ""
    localization_style: str = "Manga"
    text_style: str = "Manga"
    auto_fit: bool = True
    bubble_padding: int = 5
    max_lines: int = 3
    glossary: dict[str, str] = field(default_factory=dict)
    images: list[ImageRecord] = field(default_factory=list)
    selected_image: int = 0
    created_at: str = ""
    updated_at: str = ""

    @classmethod
    def create(cls, name: str, root: Path) -> "MangaProject":
        now = datetime.now(timezone.utc).isoformat()
        return cls(str(uuid4()), name, str(root.resolve()), created_at=now, updated_at=now)

    @property
    def root_path(self) -> Path:
        return Path(self.root)

    @property
    def project_file(self) -> Path:
        return self.root_path / "project.json"

    @property
    def artifacts(self) -> Path:
        return self.root_path / "artifacts"

    def add_sources(self, sources: list[tuple[Path, str]]) -> int:
        known = {Path(item.source_path).resolve() for item in self.images}
        used_relative = {item.relative_path.casefold() for item in self.images}
        added = 0
        for source, relative in sources:
            resolved = source.resolve()
            if resolved in known:
                continue
            candidate = Path(relative)
            counter = 2
            while candidate.as_posix().casefold() in used_relative:
                candidate = candidate.with_name(f"{Path(relative).stem}_{counter}{Path(relative).suffix}")
                counter += 1
            relative = candidate.as_posix()
            self.images.append(ImageRecord(str(uuid4()), str(resolved), relative))
            known.add(resolved)
            used_relative.add(relative.casefold())
            added += 1
        return added

    def save(self) -> None:
        self.root_path.mkdir(parents=True, exist_ok=True)
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.updated_at = datetime.now(timezone.utc).isoformat()
        payload = asdict(self)
        self.project_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "MangaProject":
        project_file = path / "project.json" if path.is_dir() else path
        payload = json.loads(project_file.read_text(encoding="utf-8"))
        images = []
        for raw in payload.get("images", []):
            edits = {key: RegionEdit(**value) for key, value in raw.pop("edits", {}).items()}
            manual_regions = [ManualRegion(**value) for value in raw.pop("manual_regions", [])]
            images.append(ImageRecord(**raw, edits=edits, manual_regions=manual_regions))
        return cls(
            id=payload["id"], name=payload["name"], root=str(project_file.parent.resolve()),
            version=PROJECT_VERSION, target_language=payload.get("target_language", "en"),
            source_language=payload.get("source_language", "auto"), quality=payload.get("quality", "Balanced"),
            literal_provider=payload.get("literal_provider", "marian"),
            localization_provider=payload.get("localization_provider", "local"),
            localization_model=payload.get("localization_model", ""),
            localization_style=payload.get("localization_style", "Manga"),
            text_style=payload.get("text_style", "Manga"), auto_fit=payload.get("auto_fit", True),
            bubble_padding=int(payload.get("bubble_padding", 5)), max_lines=int(payload.get("max_lines", 3)),
            glossary=dict(payload.get("glossary", {})), images=images,
            selected_image=payload.get("selected_image", 0), created_at=payload.get("created_at", ""),
            updated_at=payload.get("updated_at", ""),
        )
