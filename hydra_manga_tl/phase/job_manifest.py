"""Resumable chapter job manifest helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import threading
from typing import Any


MANIFEST_VERSION = 3
QUEUE_STATES = {"pending", "preprocessing", "OCR", "translating", "rendering", "review", "done", "failed"}
STALE_ACTIVE_STATES = {"preprocessing", "OCR", "ocr", "translating", "localizing", "rendering", "reconstructing", "review", "analyzing"}
STAGE_ORDER = ("preprocessing", "OCR", "translating", "rendering", "review")
STAGE_ALIASES = {
    "ocr": "OCR",
    "localizing": "translating",
    "reconstructing": "rendering",
    "analyzing": "preprocessing",
}


@dataclass
class PageJobState:
    image_id: str
    source_path: str
    state: str = "pending"
    completed_stages: list[str] = field(default_factory=list)
    stage_records: dict[str, dict[str, Any]] = field(default_factory=dict)
    stage_errors: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str = ""
    updated_at: str = ""


class JobManifest:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.pages: dict[str, PageJobState] = {}
        self.loaded_version = MANIFEST_VERSION
        self._lock = threading.RLock()

    @classmethod
    def load(cls, path: Path) -> "JobManifest":
        manifest = cls(path)
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError):
                cls._quarantine_corrupt_manifest(path)
                return manifest
            if not isinstance(payload, dict):
                cls._quarantine_corrupt_manifest(path)
                return manifest
            manifest.loaded_version = int(payload.get("version", 1) or 1)
            manifest.pages = {
                key: PageJobState(
                    image_id=str(value.get("image_id", key)),
                    source_path=str(value.get("source_path", "")),
                    state=str(value.get("state", "pending")),
                    completed_stages=list(value.get("completed_stages", [])),
                    stage_records=dict(value.get("stage_records", {})),
                    stage_errors=dict(value.get("stage_errors", {})),
                    error=str(value.get("error", "")),
                    updated_at=str(value.get("updated_at", "")),
                )
                for key, value in dict(payload.get("pages", {})).items()
                if isinstance(value, dict)
            }
        return manifest

    @staticmethod
    def _quarantine_corrupt_manifest(path: Path) -> None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = path.with_name(f"{path.name}.corrupt-{timestamp}")
        counter = 1
        while destination.exists():
            destination = path.with_name(f"{path.name}.corrupt-{timestamp}-{counter}")
            counter += 1
        try:
            path.replace(destination)
        except OSError:
            pass

    def ensure_page(self, image_id: str, source_path: str) -> PageJobState:
        page = self.pages.get(image_id)
        if page is None:
            page = PageJobState(image_id=image_id, source_path=source_path)
            self.pages[image_id] = page
        elif page.source_path != source_path:
            page.source_path = source_path
            page.completed_stages.clear()
            page.stage_records.clear()
            page.stage_errors.clear()
            page.state = "pending"
            page.error = ""
        return page

    @staticmethod
    def artifact_fingerprint(path: Path) -> dict[str, Any]:
        resolved = Path(path).resolve()
        digest = hashlib.sha256()
        size = 0
        with resolved.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return {
            "path": str(resolved),
            "size": size,
            "sha256": digest.hexdigest(),
        }

    def record_stage(
        self,
        image_id: str,
        stage: str,
        *,
        input_fingerprint: str,
        artifacts: dict[str, Path],
        metadata: dict[str, Any] | None = None,
        source_path: Path | None = None,
        application_version: str = "",
        settings_fingerprint: str = "",
        input_artifacts: dict[str, Path] | None = None,
        provider_identity: str = "",
        model_identity: str = "",
        error_summary: str = "",
    ) -> None:
        page = self.pages[image_id]
        canonical = self._canonical_stage(stage)
        metadata_payload = dict(metadata or {})
        metadata_payload.setdefault("status", "complete")
        metadata_payload.setdefault(
            "pipeline_version",
            str(application_version or ""),
        )
        metadata_payload.setdefault("duration_ms", 0)
        metadata_payload.setdefault(
            "dependencies",
            {
                str(name): str(path)
                for name, path in (input_artifacts or {}).items()
            },
        )
        output_fingerprints = {
            str(name): self.artifact_fingerprint(path)
            for name, path in artifacts.items()
        }
        page.stage_records[canonical] = {
            "contract_version": MANIFEST_VERSION,
            "stage": canonical,
            "source": (
                self.artifact_fingerprint(source_path)
                if source_path is not None
                else {}
            ),
            "application_version": str(application_version),
            "settings_fingerprint": str(settings_fingerprint),
            "input_fingerprint": str(input_fingerprint),
            "input_artifacts": {
                str(name): self.artifact_fingerprint(path)
                for name, path in (input_artifacts or {}).items()
            },
            # Keep ``artifacts`` for v1/v2 readers while exposing the explicit
            # v3 output name for support tools and future migrations.
            "artifacts": output_fingerprints,
            "output_artifacts": output_fingerprints,
            "provider_identity": str(provider_identity),
            "model_identity": str(model_identity),
            "metadata": metadata_payload,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "error_summary": str(error_summary),
        }
        page.stage_errors.pop(canonical, None)
        if canonical not in page.completed_stages:
            page.completed_stages.append(canonical)
        page.updated_at = datetime.now(timezone.utc).isoformat()
        self.save()

    def stage_reusable(
        self,
        image_id: str,
        stage: str,
        *,
        input_fingerprint: str,
        artifacts: dict[str, Path] | None = None,
        source_path: Path | None = None,
        application_version: str | None = None,
        settings_fingerprint: str | None = None,
        input_artifacts: dict[str, Path] | None = None,
        provider_identity: str | None = None,
        model_identity: str | None = None,
    ) -> bool:
        page = self.pages.get(image_id)
        if page is None:
            return False
        record = page.stage_records.get(self._canonical_stage(stage))
        if not isinstance(record, dict):
            return False
        strict_contract = any(value is not None for value in (
            source_path,
            application_version,
            settings_fingerprint,
            input_artifacts,
            provider_identity,
            model_identity,
        ))
        if strict_contract and int(record.get("contract_version", 0) or 0) != MANIFEST_VERSION:
            return False
        if str(record.get("input_fingerprint", "")) != str(input_fingerprint):
            return False
        if source_path is not None and not self._artifact_matches(
            record.get("source", {}),
            Path(source_path),
        ):
            return False
        if (
            application_version is not None
            and str(record.get("application_version", ""))
            != str(application_version)
        ):
            return False
        if (
            settings_fingerprint is not None
            and str(record.get("settings_fingerprint", ""))
            != str(settings_fingerprint)
        ):
            return False
        if (
            provider_identity is not None
            and str(record.get("provider_identity", ""))
            != str(provider_identity)
        ):
            return False
        if (
            model_identity is not None
            and str(record.get("model_identity", ""))
            != str(model_identity)
        ):
            return False
        if input_artifacts is not None and not self._artifact_set_matches(
            record.get("input_artifacts", {}),
            input_artifacts,
        ):
            return False
        recorded_artifacts = (
            record.get("output_artifacts")
            or record.get("artifacts", {})
        )
        if not isinstance(recorded_artifacts, dict) or not recorded_artifacts:
            return False
        expected = artifacts or {}
        if expected and set(expected) != set(recorded_artifacts):
            return False
        return self._artifact_set_matches(recorded_artifacts, expected or None)

    @classmethod
    def _artifact_matches(
        cls,
        payload: Any,
        expected: Path | None = None,
    ) -> bool:
        if not isinstance(payload, dict):
            return False
        recorded_path = Path(str(payload.get("path", ""))).resolve()
        path = Path(expected).resolve() if expected is not None else recorded_path
        if expected is not None and path != recorded_path:
            return False
        if not path.is_file():
            return False
        try:
            current = cls.artifact_fingerprint(path)
        except OSError:
            return False
        return (
            current["size"] == int(payload.get("size", -1))
            and current["sha256"] == str(payload.get("sha256", ""))
        )

    @classmethod
    def _artifact_set_matches(
        cls,
        recorded: Any,
        expected: dict[str, Path] | None,
    ) -> bool:
        if not isinstance(recorded, dict) or not recorded:
            return False
        if expected is not None and set(expected) != set(recorded):
            return False
        for name, payload in recorded.items():
            path = expected.get(name) if expected is not None else None
            if not cls._artifact_matches(payload, path):
                return False
        return True

    def stage_artifacts_valid(self, image_id: str, stage: str) -> bool:
        page = self.pages.get(image_id)
        if page is None:
            return False
        record = page.stage_records.get(self._canonical_stage(stage))
        if not isinstance(record, dict):
            return False
        input_fingerprint = str(record.get("input_fingerprint", ""))
        if not input_fingerprint:
            return False
        return self.stage_reusable(
            image_id,
            stage,
            input_fingerprint=input_fingerprint,
        )

    def invalidate_from(self, image_id: str, stage: str) -> None:
        page = self.pages.get(image_id)
        if page is None:
            return
        canonical = self._canonical_stage(stage)
        try:
            first = STAGE_ORDER.index(canonical)
        except ValueError:
            first = 0
        invalid = set(STAGE_ORDER[first:])
        page.stage_records = {
            key: value
            for key, value in page.stage_records.items()
            if self._canonical_stage(key) not in invalid
        }
        page.completed_stages = [
            value
            for value in page.completed_stages
            if self._canonical_stage(value) not in invalid
        ]
        page.stage_errors = {
            key: value
            for key, value in page.stage_errors.items()
            if self._canonical_stage(key) not in invalid
        }
        if page.state == "done" or self._canonical_stage(page.state) in invalid:
            page.state = "partial" if first > 1 else "queued"
        page.error = ""
        page.updated_at = datetime.now(timezone.utc).isoformat()
        self.save()

    @staticmethod
    def _canonical_stage(stage: str) -> str:
        value = str(stage)
        return STAGE_ALIASES.get(value, value)

    def mark(self, image_id: str, state: str, *, stage: str | None = None, error: str = "") -> None:
        page = self.pages[image_id]
        previous_state = page.state
        page.state = state
        page.error = error
        page.updated_at = datetime.now(timezone.utc).isoformat()
        if state == "failed" and error:
            failed_stage = self._canonical_stage(stage or previous_state)
            page.stage_errors[failed_stage] = {
                "error_summary": str(error),
                "failed_at": page.updated_at,
            }
        if stage and stage not in page.completed_stages:
            page.completed_stages.append(stage)
        self.save()

    def recover_stale(self, checkpoint_exists=None) -> dict[str, str]:
        """Normalize interrupted work while preserving completed checkpoints."""
        recovered: dict[str, str] = {}
        for image_id, page in self.pages.items():
            if page.state not in STALE_ACTIVE_STATES:
                continue
            has_ocr = bool(checkpoint_exists(image_id, "OCR")) if checkpoint_exists else "OCR" in page.completed_stages
            page.state = "partial" if has_ocr else "queued"
            page.error = ""
            page.updated_at = datetime.now(timezone.utc).isoformat()
            recovered[image_id] = page.state
        if recovered:
            self.save()
        return recovered

    def save(self) -> None:
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {
                "version": MANIFEST_VERSION,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "pages": {key: asdict(value) for key, value in self.pages.items()},
            }
            temporary = self.path.with_suffix(self.path.suffix + ".tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.path)
