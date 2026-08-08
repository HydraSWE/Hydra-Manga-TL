"""Read-only project metadata and compatibility decisions."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

from hydra_manga_tl import __version__


OLDEST_MIGRATABLE_SCHEMA = 4
SCHEMA_MINIMUM_APP_VERSION = {
    4: "0.6.0",
    5: "0.7.0",
    6: "0.8.0-alpha",
    7: "0.9.0",
    8: "1.0.0",
    9: __version__,
    10: __version__,
}


class ProjectCompatibilityError(ValueError):
    """Base class for project compatibility failures."""


class InvalidProjectError(ProjectCompatibilityError):
    """The project metadata cannot be read or validated."""


class IncompatibleProjectError(ProjectCompatibilityError):
    """The project requires a newer or otherwise unsupported schema."""

    def __init__(self, metadata: "ProjectMetadata", message: str) -> None:
        super().__init__(message)
        self.metadata = metadata


class ProjectMigrationRequired(ProjectCompatibilityError):
    """The project is older and requires an explicitly approved migration."""

    def __init__(self, metadata: "ProjectMetadata") -> None:
        super().__init__(
            f'Project "{metadata.name}" uses schema {metadata.schema} and must '
            f"be upgraded to schema {metadata.current_schema} before it can open."
        )
        self.metadata = metadata


@dataclass(frozen=True)
class ProjectMetadata:
    path: Path
    name: str
    schema: int
    current_schema: int
    minimum_supported_schema: int
    created_by: str
    last_saved_by: str
    minimum_app_version: str
    created_at: str
    updated_at: str
    page_count: int
    source_language: str
    target_language: str
    status: str
    message: str = ""

    @property
    def compatible(self) -> bool:
        return self.status in {"compatible", "migration_required"}

    @property
    def migration_required(self) -> bool:
        return self.status == "migration_required"


def project_file_for(path: Path) -> Path:
    candidate = Path(path)
    return candidate / "project.json" if candidate.is_dir() else candidate


def _schema_from_payload(payload: dict) -> int:
    raw_schema = payload.get("project_schema", payload.get("version"))
    if raw_schema is None:
        # Projects predating explicit schemas use the oldest supported loader.
        return OLDEST_MIGRATABLE_SCHEMA
    if isinstance(raw_schema, bool):
        raise InvalidProjectError("Project schema must be a positive integer.")
    try:
        schema = int(raw_schema)
    except (TypeError, ValueError) as error:
        raise InvalidProjectError("Project schema must be a positive integer.") from error
    if schema < 1:
        raise InvalidProjectError("Project schema must be a positive integer.")
    return schema


def read_project_payload(path: Path) -> tuple[Path, dict]:
    """Read JSON without constructing project, image, OCR, or workspace objects."""
    project_file = project_file_for(path)
    try:
        payload = json.loads(project_file.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InvalidProjectError(f"Project file was not found: {project_file}") from error
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise InvalidProjectError(f"Project metadata could not be read: {error}") from error
    if not isinstance(payload, dict):
        raise InvalidProjectError("Project file must contain a JSON object.")
    if not isinstance(payload.get("id"), str) or not payload.get("id", "").strip():
        raise InvalidProjectError("Project metadata is missing a valid id.")
    if not isinstance(payload.get("name"), str) or not payload.get("name", "").strip():
        raise InvalidProjectError("Project metadata is missing a valid name.")
    return project_file, payload


def inspect_project(path: Path, *, current_schema: int) -> ProjectMetadata:
    project_file, payload = read_project_payload(path)
    schema = _schema_from_payload(payload)
    images = payload.get("images", [])
    page_count = len(images) if isinstance(images, list) else 0
    minimum_supported = payload.get("minimum_supported_schema", schema)
    try:
        minimum_supported = int(minimum_supported)
    except (TypeError, ValueError):
        minimum_supported = schema
    minimum_app_version = str(
        payload.get("minimum_app_version")
        or SCHEMA_MINIMUM_APP_VERSION.get(schema)
        or payload.get("last_saved_by")
        or "a newer Hydra release"
    )
    if schema > current_schema or minimum_supported > current_schema:
        status = "incompatible"
        message = (
            f"This project requires schema {max(schema, minimum_supported)}, "
            f"but Hydra {__version__} supports "
            f"up to schema {current_schema}. Open it with Hydra "
            f"{minimum_app_version} or newer."
        )
    elif schema < OLDEST_MIGRATABLE_SCHEMA:
        status = "unsupported"
        message = (
            f"This project uses unsupported schema {schema}. The oldest schema "
            f"Hydra can migrate is {OLDEST_MIGRATABLE_SCHEMA}."
        )
    elif schema < current_schema:
        status = "migration_required"
        message = (
            f"This project must be upgraded from schema {schema} to "
            f"schema {current_schema}. A backup will be created first."
        )
    else:
        status = "compatible"
        message = "Compatible"
    return ProjectMetadata(
        path=project_file,
        name=str(payload["name"]).strip(),
        schema=schema,
        current_schema=current_schema,
        minimum_supported_schema=minimum_supported,
        created_by=str(payload.get("created_by") or "Unknown"),
        last_saved_by=str(payload.get("last_saved_by") or "Unknown"),
        minimum_app_version=minimum_app_version,
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
        page_count=page_count,
        source_language=str(payload.get("source_language") or "auto"),
        target_language=str(payload.get("target_language") or "en"),
        status=status,
        message=message,
    )


def require_compatible_project(path: Path, *, current_schema: int) -> ProjectMetadata:
    metadata = inspect_project(path, current_schema=current_schema)
    if metadata.status in {"incompatible", "unsupported"}:
        raise IncompatibleProjectError(metadata, metadata.message)
    if metadata.migration_required:
        raise ProjectMigrationRequired(metadata)
    return metadata
