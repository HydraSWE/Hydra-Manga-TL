"""Project migration registry, backups, and rollback."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Callable
from zipfile import ZIP_DEFLATED, ZipFile

from hydra_manga_tl import __version__
from hydra_manga_tl.project.compatibility import (
    InvalidProjectError,
    ProjectMetadata,
    inspect_project,
    read_project_payload,
)


MigrationTransform = Callable[[dict], dict]


@dataclass(frozen=True)
class MigrationStep:
    source_schema: int
    target_schema: int
    transform: MigrationTransform
    description: str


@dataclass(frozen=True)
class MigrationResult:
    project_file: Path
    source_schema: int
    target_schema: int
    backup_path: Path
    steps: tuple[MigrationStep, ...]


class MigrationRegistry:
    def __init__(self) -> None:
        self._steps: dict[int, MigrationStep] = {}

    def register(self, step: MigrationStep) -> None:
        if step.target_schema != step.source_schema + 1:
            raise ValueError("Project migrations must advance exactly one schema.")
        if step.source_schema in self._steps:
            raise ValueError(f"Schema {step.source_schema} already has a migration.")
        self._steps[step.source_schema] = step

    def path(self, source_schema: int, target_schema: int) -> tuple[MigrationStep, ...]:
        steps: list[MigrationStep] = []
        schema = source_schema
        while schema < target_schema:
            step = self._steps.get(schema)
            if step is None:
                raise InvalidProjectError(
                    f"No migration is available from schema {schema} to "
                    f"schema {schema + 1}."
                )
            steps.append(step)
            schema = step.target_schema
        return tuple(steps)


def _ensure_project_defaults(payload: dict) -> dict:
    payload.setdefault("target_language", "en")
    payload.setdefault("source_language", "auto")
    payload.setdefault("quality", "Balanced")
    payload.setdefault("literal_provider", "marian")
    payload.setdefault("localization_provider", "local")
    payload.setdefault("localization_model", "")
    payload.setdefault("localization_style", "Manga")
    payload.setdefault("text_style", "Manga")
    payload.setdefault("auto_fit", True)
    payload.setdefault("bubble_padding", 5)
    payload.setdefault("max_lines", 3)
    payload.setdefault("glossary", {})
    payload.setdefault("images", [])
    payload.setdefault("selected_image", 0)
    return payload


def _schema_4_to_5(payload: dict) -> dict:
    return _ensure_project_defaults(payload)


def _schema_5_to_6(payload: dict) -> dict:
    payload = _ensure_project_defaults(payload)
    for image in payload.get("images", []):
        if isinstance(image, dict):
            image.setdefault("manual_regions", [])
            image.setdefault("suppressed_auto_group_indices", [])
    return payload


def _schema_6_to_7(payload: dict) -> dict:
    payload = _ensure_project_defaults(payload)
    payload.setdefault("filmstrip_visible", True)
    for image in payload.get("images", []):
        if isinstance(image, dict):
            image.setdefault("ai_subject_ids", {})
            image.setdefault("approved_ai_subject_ids", [])
    return payload


def _schema_7_to_8(payload: dict) -> dict:
    payload = _ensure_project_defaults(payload)
    active_target = str(payload.get("target_language") or "en").strip().casefold() or "en"
    payload.setdefault("target_languages", [active_target])
    for image in payload.get("images", []):
        if isinstance(image, dict):
            image.setdefault("reading_order", [])
            image.setdefault("target_states", {})
    return payload


def _rect_to_polygon(rect: list) -> list[list[int]]:
    if not isinstance(rect, list) or len(rect) != 4:
        return []
    try:
        x1, y1, x2, y2 = [int(value) for value in rect]
    except (TypeError, ValueError):
        return []
    return [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]


def _normalize_polygon(value: object) -> list[list[int]]:
    if not isinstance(value, list):
        return []
    polygon: list[list[int]] = []
    for point in value:
        if not isinstance(point, list) or len(point) < 2:
            continue
        try:
            polygon.append([int(point[0]), int(point[1])])
        except (TypeError, ValueError):
            continue
    return polygon


def _normalize_polygons(value: object) -> list[list[list[int]]]:
    if not isinstance(value, list):
        return []
    polygons: list[list[list[int]]] = []
    for polygon in value:
        normalized = _normalize_polygon(polygon)
        if normalized:
            polygons.append(normalized)
    return polygons


def _upgrade_manual_region_geometry(region: dict) -> None:
    selection = (
        _normalize_polygon(region.get("selection_polygon"))
        or _normalize_polygon(region.get("polygon"))
        or _rect_to_polygon(region.get("rect", []))
    )
    cleanup = (
        _normalize_polygons(region.get("cleanup_polygons"))
        or _normalize_polygons(region.get("source_polygons"))
        or ([selection] if selection else [])
    )
    placement = (
        _normalize_polygon(region.get("placement_polygon"))
        or selection
    )
    region["selection_polygon"] = selection
    region["cleanup_polygons"] = cleanup
    region["placement_polygon"] = placement
    if selection:
        region["polygon"] = selection


def _upgrade_manual_regions(regions: object) -> None:
    if not isinstance(regions, list):
        return
    for region in regions:
        if isinstance(region, dict):
            _upgrade_manual_region_geometry(region)


def _schema_8_to_9(payload: dict) -> dict:
    payload = _ensure_project_defaults(payload)
    for image in payload.get("images", []):
        if not isinstance(image, dict):
            continue
        _upgrade_manual_regions(image.get("manual_regions", []))
        target_states = image.get("target_states", {})
        if isinstance(target_states, dict):
            for state in target_states.values():
                if isinstance(state, dict):
                    _upgrade_manual_regions(state.get("manual_regions", []))
    return payload


def _schema_9_to_10(payload: dict) -> dict:
    payload = _ensure_project_defaults(payload)
    for image in payload.get("images", []):
        if not isinstance(image, dict):
            continue
        _default_preserved_marks(image.get("manual_regions", []))
        target_states = image.get("target_states", {})
        if isinstance(target_states, dict):
            for state in target_states.values():
                if isinstance(state, dict):
                    _default_preserved_marks(state.get("manual_regions", []))
    return payload


def _schema_10_to_11(payload: dict) -> dict:
    payload = _ensure_project_defaults(payload)
    for image in payload.get("images", []):
        if not isinstance(image, dict):
            continue
        _default_decorative_symbols(image.get("manual_regions", []))
        target_states = image.get("target_states", {})
        if isinstance(target_states, dict):
            for state in target_states.values():
                if isinstance(state, dict):
                    _default_decorative_symbols(state.get("manual_regions", []))
    return payload


def _default_preserved_marks(regions: object) -> None:
    if not isinstance(regions, list):
        return
    for region in regions:
        if isinstance(region, dict):
            region.setdefault("preserved_marks", [])


def _default_decorative_symbols(regions: object) -> None:
    if not isinstance(regions, list):
        return
    for region in regions:
        if isinstance(region, dict):
            region.setdefault("decorative_symbols", [])


MIGRATIONS = MigrationRegistry()
MIGRATIONS.register(MigrationStep(4, 5, _schema_4_to_5, "Add provider and layout defaults"))
MIGRATIONS.register(MigrationStep(5, 6, _schema_5_to_6, "Add manual-region state"))
MIGRATIONS.register(MigrationStep(6, 7, _schema_6_to_7, "Add filmstrip and AI-review state"))
MIGRATIONS.register(MigrationStep(7, 8, _schema_7_to_8, "Add reading order and target states"))
MIGRATIONS.register(MigrationStep(8, 9, _schema_8_to_9, "Add intelligent cleanup geometry"))
MIGRATIONS.register(MigrationStep(9, 10, _schema_9_to_10, "Add manual preserved mark metadata"))
MIGRATIONS.register(MigrationStep(10, 11, _schema_10_to_11, "Add redrawable decorative symbol metadata"))


class MigrationManager:
    def __init__(self, registry: MigrationRegistry = MIGRATIONS) -> None:
        self.registry = registry

    @staticmethod
    def _backup(project_file: Path, schema: int, original: bytes) -> Path:
        backups = project_file.parent / "backups"
        backups.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S_%fZ")
        backup = backups / f"project_schema{schema}_{timestamp}.zip"
        with ZipFile(backup, "x", compression=ZIP_DEFLATED) as archive:
            archive.writestr("project.json", original)
        return backup

    def migrate(self, path: Path, *, target_schema: int) -> MigrationResult:
        project_file, payload = read_project_payload(path)
        metadata = inspect_project(project_file, current_schema=target_schema)
        if metadata.schema >= target_schema:
            raise InvalidProjectError(
                f"Schema {metadata.schema} does not require migration to "
                f"schema {target_schema}."
            )
        steps = self.registry.path(metadata.schema, target_schema)
        original = project_file.read_bytes()
        backup = self._backup(project_file, metadata.schema, original)
        working = json.loads(original.decode("utf-8"))
        history = list(working.get("migration_history", []))
        try:
            for step in steps:
                working = step.transform(working)
                if not isinstance(working, dict):
                    raise InvalidProjectError(
                        f"Migration {step.source_schema} to "
                        f"{step.target_schema} returned invalid data."
                    )
                history.append({
                    "from_schema": step.source_schema,
                    "to_schema": step.target_schema,
                    "application_version": __version__,
                    "migrated_at": datetime.now(timezone.utc).isoformat(),
                    "backup": str(backup),
                })
                working["version"] = step.target_schema
                working["project_schema"] = step.target_schema
            working["minimum_supported_schema"] = target_schema
            working["minimum_app_version"] = __version__
            working.setdefault("created_by", metadata.created_by)
            working["last_saved_by"] = __version__
            working["migration_history"] = history
            encoded = json.dumps(working, ensure_ascii=False, indent=2).encode("utf-8")
            temporary = project_file.with_name(f"{project_file.name}.migration.tmp")
            temporary.write_bytes(encoded)
            temporary.replace(project_file)
        except Exception:
            project_file.write_bytes(original)
            temporary = project_file.with_name(f"{project_file.name}.migration.tmp")
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            raise
        return MigrationResult(
            project_file=project_file,
            source_schema=metadata.schema,
            target_schema=target_schema,
            backup_path=backup,
            steps=steps,
        )

    @staticmethod
    def restore(result: MigrationResult) -> None:
        """Restore the pre-migration project JSON from its retained backup."""
        with ZipFile(result.backup_path, "r") as archive:
            original = archive.read("project.json")
        temporary = result.project_file.with_name(
            f"{result.project_file.name}.restore.tmp"
        )
        try:
            temporary.write_bytes(original)
            temporary.replace(result.project_file)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def migration_message(metadata: ProjectMetadata) -> str:
    return (
        f'"{metadata.name}" uses project schema {metadata.schema}.\n\n'
        f"Hydra will create a backup and upgrade it to schema "
        f"{metadata.current_schema}. Older Hydra versions may no longer be "
        "able to open the upgraded project.\n\nUpgrade this project now?"
    )
