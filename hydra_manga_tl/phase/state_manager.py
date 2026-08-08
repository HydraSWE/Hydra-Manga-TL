"""Authoritative stage planning and validation for the Hydra pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
import time
from typing import Any, Iterable

from hydra_manga_tl import __version__
from hydra_manga_tl.phase.job_manifest import JobManifest


class StageAction(str, Enum):
    SKIP = "skip"
    EXECUTE = "execute"
    BLOCKED = "blocked"
    FAILED = "failed"
    INVALIDATED = "invalidated"


@dataclass(frozen=True)
class StageContract:
    """Declarative contract for one pipeline stage."""

    name: str
    requires: tuple[str, ...] = ()
    outputs: tuple[str, ...] = ()
    invalidated_by: tuple[str, ...] = ()


@dataclass(frozen=True)
class StageValidationRequest:
    """All data needed to decide whether a recorded stage is reusable."""

    input_fingerprint: str
    artifacts: dict[str, Path] = field(default_factory=dict)
    input_artifacts: dict[str, Path] = field(default_factory=dict)
    source_path: Path | None = None
    application_version: str | None = __version__
    settings_fingerprint: str | None = None
    provider_identity: str | None = None
    model_identity: str | None = None


@dataclass(frozen=True)
class StagePlanStep:
    stage: str
    action: StageAction
    reason: str = ""


@dataclass(frozen=True)
class PagePipelinePlan:
    image_id: str
    steps: tuple[StagePlanStep, ...]

    def action_for(self, stage: str) -> StageAction | None:
        canonical = PipelineDependencyGraph.canonical(stage)
        for step in self.steps:
            if PipelineDependencyGraph.canonical(step.stage) == canonical:
                return step.action
        return None

    @property
    def executable_stages(self) -> tuple[str, ...]:
        return tuple(
            step.stage
            for step in self.steps
            if step.action is StageAction.EXECUTE
        )


@dataclass(frozen=True)
class StageValidationResult:
    stage: str
    reusable: bool
    reason: str = ""


class PipelineDependencyGraph:
    """Ordered stage graph used for generic downstream invalidation."""

    _ALIASES = {
        "analyzing": "preprocessing",
        "ocr": "OCR",
        "localizing": "translating",
        "translation": "translating",
        "render": "rendering",
        "reconstructing": "rendering",
        "review": "review",
        "export": "export",
    }

    def __init__(self, contracts: Iterable[StageContract]) -> None:
        self.contracts = {
            self.canonical(contract.name): StageContract(
                name=self.canonical(contract.name),
                requires=tuple(self.canonical(value) for value in contract.requires),
                outputs=tuple(contract.outputs),
                invalidated_by=tuple(contract.invalidated_by),
            )
            for contract in contracts
        }
        self._order = self._topological_order()

    @classmethod
    def default(cls) -> "PipelineDependencyGraph":
        return cls((
            StageContract("preprocessing"),
            StageContract("OCR", requires=("preprocessing",)),
            StageContract("translating", requires=("OCR",)),
            StageContract("rendering", requires=("translating",)),
            StageContract("review", requires=("rendering",)),
            StageContract("export", requires=("review",)),
        ))

    @staticmethod
    def canonical(stage: str) -> str:
        value = str(stage)
        return PipelineDependencyGraph._ALIASES.get(value, value)

    @property
    def order(self) -> tuple[str, ...]:
        return self._order

    def downstream_from(self, stage: str) -> tuple[str, ...]:
        canonical = self.canonical(stage)
        if canonical not in self.contracts:
            return self.order
        affected: set[str] = {canonical}
        changed = True
        while changed:
            changed = False
            for candidate, contract in self.contracts.items():
                if candidate in affected:
                    continue
                if any(required in affected for required in contract.requires):
                    affected.add(candidate)
                    changed = True
        return tuple(item for item in self.order if item in affected)

    def _topological_order(self) -> tuple[str, ...]:
        ordered: list[str] = []
        temporary: set[str] = set()
        permanent: set[str] = set()

        def visit(stage: str) -> None:
            if stage in permanent:
                return
            if stage in temporary:
                raise ValueError(f"Pipeline stage dependency cycle at {stage}")
            temporary.add(stage)
            contract = self.contracts[stage]
            for required in contract.requires:
                if required not in self.contracts:
                    raise ValueError(
                        f"Pipeline stage {stage} requires unknown stage {required}"
                    )
                visit(required)
            temporary.remove(stage)
            permanent.add(stage)
            ordered.append(stage)

        for stage in self.contracts:
            visit(stage)
        return tuple(ordered)


class PipelineValidator:
    """Validates manifest records before the state manager plans work."""

    def __init__(self, manifest: JobManifest) -> None:
        self.manifest = manifest

    def validate(
        self,
        image_id: str,
        stage: str,
        request: StageValidationRequest,
    ) -> StageValidationResult:
        reusable = self.manifest.stage_reusable(
            image_id,
            stage,
            input_fingerprint=request.input_fingerprint,
            artifacts=request.artifacts,
            source_path=request.source_path,
            application_version=request.application_version,
            settings_fingerprint=request.settings_fingerprint,
            input_artifacts=request.input_artifacts or None,
            provider_identity=request.provider_identity,
            model_identity=request.model_identity,
        )
        return StageValidationResult(
            stage=PipelineDependencyGraph.canonical(stage),
            reusable=reusable,
            reason="" if reusable else "record_missing_or_dependencies_changed",
        )

    def recover_stale(self, checkpoint_exists=None) -> dict[str, str]:
        return self.manifest.recover_stale(checkpoint_exists)

    def repair_stage_metadata_defaults(self) -> int:
        repaired = 0
        for page in self.manifest.pages.values():
            for record in page.stage_records.values():
                if not isinstance(record, dict):
                    continue
                metadata = record.get("metadata")
                if not isinstance(metadata, dict):
                    metadata = {}
                    record["metadata"] = metadata
                before = dict(metadata)
                metadata.setdefault("status", "complete")
                metadata.setdefault(
                    "pipeline_version",
                    str(
                        record.get("application_version")
                        or record.get("pipeline_version")
                        or ""
                    ),
                )
                metadata.setdefault("duration_ms", 0)
                input_artifacts = record.get("input_artifacts", {})
                dependencies: dict[str, str] = {}
                if isinstance(input_artifacts, dict):
                    for name, artifact in input_artifacts.items():
                        if isinstance(artifact, dict):
                            dependencies[str(name)] = str(
                                artifact.get("path", "")
                            )
                metadata.setdefault("dependencies", dependencies)
                if metadata != before:
                    repaired += 1
        if repaired:
            self.manifest.save()
        return repaired


class PipelineStateManager:
    """The sole authority for stage planning, resume, and invalidation."""

    def __init__(
        self,
        manifest: JobManifest,
        *,
        graph: PipelineDependencyGraph | None = None,
        validator: PipelineValidator | None = None,
    ) -> None:
        self.manifest = manifest
        self.graph = graph or PipelineDependencyGraph.default()
        self.validator = validator or PipelineValidator(manifest)

    def plan_page(
        self,
        image_id: str,
        source_path: Path | str,
        validations: dict[str, StageValidationRequest],
        *,
        force_from: str | None = None,
    ) -> PagePipelinePlan:
        self.manifest.ensure_page(image_id, str(source_path))
        if force_from:
            self.invalidate_from(image_id, force_from)

        steps: list[StagePlanStep] = []
        downstream_execute = False
        for stage in self.graph.order:
            request = validations.get(stage) or validations.get(
                PipelineDependencyGraph.canonical(stage)
            )
            if downstream_execute:
                steps.append(StagePlanStep(stage, StageAction.EXECUTE, "upstream_execute"))
                continue
            if request is None:
                steps.append(StagePlanStep(stage, StageAction.BLOCKED, "missing_validation_request"))
                downstream_execute = True
                continue
            result = self.validator.validate(image_id, stage, request)
            if result.reusable:
                steps.append(StagePlanStep(stage, StageAction.SKIP, "reusable"))
            else:
                self.invalidate_from(image_id, stage)
                steps.append(StagePlanStep(stage, StageAction.EXECUTE, result.reason))
                downstream_execute = True
        return PagePipelinePlan(image_id=image_id, steps=tuple(steps))

    def invalidate_from(self, image_id: str, stage: str) -> tuple[str, ...]:
        page = self.manifest.pages.get(image_id)
        if page is None:
            return ()
        affected = set(self.graph.downstream_from(stage))
        page.stage_records = {
            key: value
            for key, value in page.stage_records.items()
            if PipelineDependencyGraph.canonical(key) not in affected
        }
        page.completed_stages = [
            value
            for value in page.completed_stages
            if PipelineDependencyGraph.canonical(value) not in affected
        ]
        page.stage_errors = {
            key: value
            for key, value in page.stage_errors.items()
            if PipelineDependencyGraph.canonical(key) not in affected
        }
        page.error = ""
        page.state = "partial" if "OCR" not in affected else "queued"
        page.updated_at = datetime.now(timezone.utc).isoformat()
        self.manifest.save()
        return tuple(item for item in self.graph.order if item in affected)

    def record_stage_completion(
        self,
        image_id: str,
        stage: str,
        request: StageValidationRequest,
        *,
        duration_ms: int | None = None,
        metadata: dict[str, Any] | None = None,
        started_at: float | None = None,
    ) -> None:
        elapsed_ms = duration_ms
        if elapsed_ms is None and started_at is not None:
            elapsed_ms = int((time.perf_counter() - started_at) * 1000)
        stage_metadata = {
            "status": "complete",
            "pipeline_version": __version__,
            "duration_ms": int(elapsed_ms or 0),
            "dependencies": {
                name: str(path)
                for name, path in request.input_artifacts.items()
            },
            **dict(metadata or {}),
        }
        self.manifest.record_stage(
            image_id,
            stage,
            input_fingerprint=request.input_fingerprint,
            artifacts=request.artifacts,
            metadata=stage_metadata,
            source_path=request.source_path,
            application_version=request.application_version or "",
            settings_fingerprint=request.settings_fingerprint or "",
            input_artifacts=request.input_artifacts,
            provider_identity=request.provider_identity or "",
            model_identity=request.model_identity or "",
        )

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
        duration_ms: int | None = None,
    ) -> None:
        request = StageValidationRequest(
            input_fingerprint=input_fingerprint,
            artifacts=artifacts,
            input_artifacts=input_artifacts or {},
            source_path=source_path,
            application_version=application_version,
            settings_fingerprint=settings_fingerprint,
            provider_identity=provider_identity,
            model_identity=model_identity,
        )
        self.record_stage_completion(
            image_id,
            stage,
            request,
            duration_ms=duration_ms,
            metadata=metadata,
        )
