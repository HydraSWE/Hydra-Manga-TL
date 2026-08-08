"""Versioned unified Manga TL project schema."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
import json
from pathlib import Path
from uuid import uuid4

from hydra_manga_tl import __version__
from hydra_manga_tl.project.editor import RegionEdit
from hydra_manga_tl.project.compatibility import (
    IncompatibleProjectError,
    inspect_project,
    read_project_payload,
)


PROJECT_VERSION = 11
STAGES = {
    "pending", "queued", "partial", "preprocessing", "analyzing", "OCR", "ocr", "translating",
    "localizing", "rendering", "reconstructing", "review", "done", "ready", "failed", "cancelled",
}


def _rect_to_polygon(rect: list[int]) -> list[list[int]]:
    x1, y1, x2, y2 = [int(value) for value in rect]
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def _normalize_polygon(polygon: list | tuple) -> list[list[int]]:
    normalized: list[list[int]] = []
    for point in polygon or []:
        if not isinstance(point, (list, tuple)) or len(point) < 2:
            continue
        normalized.append([int(point[0]), int(point[1])])
    return normalized


def _normalize_polygons(polygons: list | tuple) -> list[list[list[int]]]:
    normalized: list[list[list[int]]] = []
    for polygon in polygons or []:
        candidate = _normalize_polygon(polygon)
        if candidate:
            normalized.append(candidate)
    return normalized


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
    polygon: list[list[int]] = field(default_factory=list)
    selection_polygon: list[list[int]] = field(default_factory=list)
    cleanup_polygons: list[list[list[int]]] = field(default_factory=list)
    placement_polygon: list[list[int]] = field(default_factory=list)
    bubble_type: str = "dialogue"
    render_mode: str = ""
    title_composition: dict = field(default_factory=dict)
    title_reconstruction: dict = field(default_factory=dict)
    style_profile: dict | None = None
    decorative_symbols: list[dict] = field(default_factory=list)
    preserved_marks: list[dict] = field(default_factory=list)
    source_text_hash: str = ""
    source_region_hash: str | None = None
    translation_source: str = ""
    translation_provider: str = ""
    translation_memory_units: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.rect = [int(value) for value in self.rect]
        self.source_polygons = _normalize_polygons(self.source_polygons)
        self.polygon = _normalize_polygon(self.polygon) or _rect_to_polygon(self.rect)
        self.selection_polygon = _normalize_polygon(self.selection_polygon) or self.polygon
        self.cleanup_polygons = (
            _normalize_polygons(self.cleanup_polygons)
            or self.source_polygons
            or [self.selection_polygon]
        )
        self.placement_polygon = _normalize_polygon(self.placement_polygon) or self.selection_polygon
        self.polygon = self.selection_polygon
        self.bubble_type = str(self.bubble_type or "dialogue")
        self.decorative_symbols = [
            dict(symbol)
            for symbol in self.decorative_symbols
            if isinstance(symbol, dict)
        ]
        self.preserved_marks = [
            dict(mark)
            for mark in self.preserved_marks
            if isinstance(mark, dict)
        ]

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
    ai_subject_ids: dict[str, str] = field(default_factory=dict)
    approved_ai_subject_ids: list[str] = field(default_factory=list)
    reading_order: list[str] = field(default_factory=list)
    target_states: dict[str, dict] = field(default_factory=dict)
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

    def sync_target_state(self, target: str) -> None:
        key = str(target).strip().casefold() or "en"
        self.target_states[key] = {
            "status": self.status,
            "error": self.error,
            "translation_result": self.translation_result,
            "rendered_image": self.rendered_image,
            "preview_image": self.preview_image,
            "edits": {
                name: asdict(edit) for name, edit in self.edits.items()
            },
            "manual_regions": [
                asdict(region) for region in self.manual_regions
            ],
            "suppressed_auto_group_indices": list(
                self.suppressed_auto_group_indices
            ),
            "approved_ai_subject_ids": list(self.approved_ai_subject_ids),
        }

    def activate_target_state(self, target: str) -> None:
        key = str(target).strip().casefold() or "en"
        state = self.target_states.get(key)
        if state is None:
            self.status = "queued"
            self.error = ""
            self.translation_result = ""
            self.rendered_image = ""
            self.preview_image = ""
            self.edits = {}
            self.manual_regions = []
            self.suppressed_auto_group_indices = []
            self.approved_ai_subject_ids = []
            return
        self.status = str(state.get("status", "queued"))
        self.error = str(state.get("error", ""))
        self.translation_result = str(state.get("translation_result", ""))
        self.rendered_image = str(state.get("rendered_image", ""))
        self.preview_image = str(state.get("preview_image", ""))
        self.edits = {
            str(name): RegionEdit(**dict(value))
            for name, value in dict(state.get("edits", {})).items()
        }
        self.manual_regions = [
            ManualRegion(**dict(value))
            for value in list(state.get("manual_regions", []))
        ]
        self.suppressed_auto_group_indices = [
            int(value)
            for value in state.get("suppressed_auto_group_indices", [])
        ]
        self.approved_ai_subject_ids = [
            str(value)
            for value in state.get("approved_ai_subject_ids", [])
        ]

    @property
    def source(self) -> Path:
        return Path(self.source_path)


@dataclass
class MangaProject:
    id: str
    name: str
    root: str
    version: int = PROJECT_VERSION
    project_schema: int = PROJECT_VERSION
    minimum_supported_schema: int = PROJECT_VERSION
    created_by: str = __version__
    last_saved_by: str = __version__
    minimum_app_version: str = __version__
    migration_history: list[dict] = field(default_factory=list)
    target_language: str = "en"
    target_languages: list[str] = field(default_factory=lambda: ["en"])
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
    filmstrip_visible: bool = True
    recent_thumbnail: str = ""
    images: list[ImageRecord] = field(default_factory=list)
    selected_image: int = 0
    created_at: str = ""
    updated_at: str = ""
    last_exported_at: str = ""
    last_export_path: str = ""
    last_export_type: str = ""
    last_export_count: int = 0
    last_export_mode: str = ""
    last_export_format: str = ""
    _extra_fields: dict = field(default_factory=dict, repr=False, compare=False)

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
        self.version = PROJECT_VERSION
        self.project_schema = PROJECT_VERSION
        self.minimum_supported_schema = PROJECT_VERSION
        self.minimum_app_version = __version__
        self.last_saved_by = __version__
        if not self.created_by:
            self.created_by = __version__
        active = str(self.target_language).strip().casefold() or "en"
        self.target_languages = list(dict.fromkeys([
            *self.target_languages,
            active,
        ]))
        for image in self.images:
            image.sync_target_state(active)
        payload = asdict(self)
        payload.update(payload.pop("_extra_fields", {}))
        for image_payload in payload.get("images", []):
            image_payload.update(image_payload.pop("_extra_fields", {}))
        self.project_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "MangaProject":
        metadata = inspect_project(path, current_schema=PROJECT_VERSION)
        if metadata.status in {"incompatible", "unsupported"}:
            raise IncompatibleProjectError(metadata, metadata.message)
        project_file, payload = read_project_payload(path)
        images = []
        image_fields = {item.name for item in fields(ImageRecord)}
        edit_fields = {item.name for item in fields(RegionEdit)}
        manual_region_fields = {item.name for item in fields(ManualRegion)}
        for source_raw in payload.get("images", []):
            if not isinstance(source_raw, dict):
                continue
            raw = dict(source_raw)
            edits = {
                key: RegionEdit(**{
                    name: item
                    for name, item in value.items()
                    if name in edit_fields
                })
                for key, value in raw.pop("edits", {}).items()
                if isinstance(value, dict)
            }
            manual_regions = [
                ManualRegion(**{
                    name: item
                    for name, item in value.items()
                    if name in manual_region_fields
                })
                for value in raw.pop("manual_regions", [])
                if isinstance(value, dict)
            ]
            known = {key: value for key, value in raw.items() if key in image_fields}
            extra = {key: value for key, value in raw.items() if key not in image_fields}
            images.append(ImageRecord(
                **known,
                edits=edits,
                manual_regions=manual_regions,
                _extra_fields=extra,
            ))
        project_fields = {item.name for item in fields(cls)}
        serialized_fields = project_fields - {"_extra_fields"}
        extra = {
            key: value
            for key, value in payload.items()
            if key not in serialized_fields
        }
        return cls(
            id=payload["id"], name=payload["name"], root=str(project_file.parent.resolve()),
            version=PROJECT_VERSION,
            project_schema=PROJECT_VERSION,
            minimum_supported_schema=PROJECT_VERSION,
            created_by=metadata.created_by,
            last_saved_by=metadata.last_saved_by,
            minimum_app_version=metadata.minimum_app_version,
            migration_history=list(payload.get("migration_history", [])),
            target_language=payload.get("target_language", "en"),
            target_languages=list(dict.fromkeys(
                str(value).strip().casefold()
                for value in payload.get(
                    "target_languages",
                    [payload.get("target_language", "en")],
                )
                if str(value).strip()
            )) or ["en"],
            source_language=payload.get("source_language", "auto"), quality=payload.get("quality", "Balanced"),
            literal_provider=payload.get("literal_provider", "marian"),
            localization_provider=payload.get("localization_provider", "local"),
            localization_model=payload.get("localization_model", ""),
            localization_style=payload.get("localization_style", "Manga"),
            text_style=payload.get("text_style", "Manga"), auto_fit=payload.get("auto_fit", True),
            bubble_padding=int(payload.get("bubble_padding", 5)), max_lines=int(payload.get("max_lines", 3)),
            glossary=dict(payload.get("glossary", {})),
            filmstrip_visible=bool(payload.get("filmstrip_visible", True)),
            recent_thumbnail=str(payload.get("recent_thumbnail", "") or ""),
            images=images,
            selected_image=payload.get("selected_image", 0), created_at=payload.get("created_at", ""),
            updated_at=payload.get("updated_at", ""),
            last_exported_at=str(payload.get("last_exported_at") or ""),
            last_export_path=str(payload.get("last_export_path") or ""),
            last_export_type=str(payload.get("last_export_type") or ""),
            last_export_count=int(payload.get("last_export_count") or 0),
            last_export_mode=str(payload.get("last_export_mode") or ""),
            last_export_format=str(payload.get("last_export_format") or ""),
            _extra_fields=extra,
        )
