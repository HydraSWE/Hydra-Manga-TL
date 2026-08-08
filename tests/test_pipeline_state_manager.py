from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hydra_manga_tl import __version__
from hydra_manga_tl.phase.job_manifest import JobManifest
from hydra_manga_tl.phase.state_manager import (
    PipelineDependencyGraph,
    PipelineStateManager,
    StageAction,
    StageValidationRequest,
)


class PipelineStateManagerTests(unittest.TestCase):
    def _request(
        self,
        fingerprint: str,
        *,
        source: Path,
        artifacts: dict[str, Path],
        input_artifacts: dict[str, Path] | None = None,
        settings: str = "settings",
        provider: str = "",
    ) -> StageValidationRequest:
        return StageValidationRequest(
            input_fingerprint=fingerprint,
            artifacts=artifacts,
            input_artifacts=input_artifacts or {},
            source_path=source,
            application_version=__version__,
            settings_fingerprint=settings,
            provider_identity=provider,
        )

    def test_dependency_graph_invalidates_requested_stage_and_downstream(self):
        graph = PipelineDependencyGraph.default()

        self.assertEqual(
            ("translating", "rendering", "review", "export"),
            graph.downstream_from("translation"),
        )
        self.assertEqual(("rendering", "review", "export"), graph.downstream_from("render"))
        self.assertEqual(
            ("preprocessing", "OCR", "translating", "rendering", "review", "export"),
            graph.downstream_from("preprocessing"),
        )
        self.assertEqual(("OCR", "translating", "rendering", "review", "export"), graph.downstream_from("OCR"))

    def test_manifest_load_quarantines_empty_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "chapter_job_manifest.json"
            path.write_text("", encoding="utf-8")

            manifest = JobManifest.load(path)

            self.assertEqual({}, manifest.pages)
            self.assertFalse(path.exists())
            corrupt = list(Path(folder).glob("chapter_job_manifest.json.corrupt-*"))
            self.assertEqual(1, len(corrupt))
            self.assertEqual("", corrupt[0].read_text(encoding="utf-8"))

    def test_manifest_load_quarantines_non_object_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "chapter_job_manifest.json"
            path.write_text("[]", encoding="utf-8")

            manifest = JobManifest.load(path)

            self.assertEqual({}, manifest.pages)
            self.assertFalse(path.exists())
            self.assertEqual(
                1,
                len(list(Path(folder).glob("chapter_job_manifest.json.corrupt-*"))),
            )

    def test_plan_skips_verified_stages_and_executes_first_invalid_stage_downstream(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "page.png"
            source.write_bytes(b"source")
            preprocessed = root / "preprocessed.png"
            preprocessed.write_text("preprocessed", encoding="utf-8")
            ocr = root / "ocr.json"
            ocr.write_text("ocr", encoding="utf-8")
            translation = root / "translation.json"
            translation.write_text("translation", encoding="utf-8")
            render = root / "render.png"
            export = root / "chapter.cbz"

            manifest = JobManifest(root / "manifest.json")
            manager = PipelineStateManager(manifest)
            manifest.ensure_page("page", str(source))
            manager.record_stage_completion(
                "page",
                "preprocessing",
                self._request(
                    "preprocess-input",
                    source=source,
                    artifacts={"preprocessed_image": preprocessed},
                    input_artifacts={"source_image": source},
                ),
                duration_ms=8,
            )
            manager.record_stage_completion(
                "page",
                "OCR",
                self._request(
                    "ocr-input",
                    source=source,
                    artifacts={"ocr_result": ocr},
                    input_artifacts={"preprocessed_image": preprocessed},
                ),
                duration_ms=12,
            )
            manager.record_stage_completion(
                "page",
                "translating",
                self._request(
                    "translation-input",
                    source=source,
                    artifacts={"translation_result": translation},
                    input_artifacts={"ocr_result": ocr},
                    provider="groq",
                ),
                duration_ms=34,
            )

            plan = manager.plan_page(
                "page",
                source,
                {
                    "preprocessing": self._request(
                        "preprocess-input",
                        source=source,
                        artifacts={"preprocessed_image": preprocessed},
                        input_artifacts={"source_image": source},
                    ),
                    "OCR": self._request(
                        "ocr-input",
                        source=source,
                        artifacts={"ocr_result": ocr},
                        input_artifacts={"preprocessed_image": preprocessed},
                    ),
                    "translating": self._request(
                        "translation-input",
                        source=source,
                        artifacts={"translation_result": translation},
                        input_artifacts={"ocr_result": ocr},
                        provider="groq",
                    ),
                    "rendering": self._request(
                        "render-input",
                        source=source,
                        artifacts={"rendered_image": render},
                        input_artifacts={"translation_result": translation},
                    ),
                    "export": self._request(
                        "export-input",
                        source=source,
                        artifacts={"archive": export},
                        input_artifacts={"rendered_image": render},
                    ),
                },
            )

            self.assertEqual(StageAction.SKIP, plan.action_for("OCR"))
            self.assertEqual(StageAction.SKIP, plan.action_for("translation"))
            self.assertEqual(StageAction.EXECUTE, plan.action_for("render"))
            self.assertEqual(StageAction.EXECUTE, plan.action_for("review"))
            self.assertEqual(StageAction.EXECUTE, plan.action_for("export"))
            self.assertEqual(("rendering", "review", "export"), plan.executable_stages)

    def test_state_manager_invalidation_uses_graph_including_export(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "page.png"
            source.write_bytes(b"source")
            artifacts = {
                "preprocessing": root / "preprocessed.png",
                "OCR": root / "ocr.json",
                "translating": root / "translation.json",
                "rendering": root / "render.png",
                "review": root / "review.json",
                "export": root / "chapter.cbz",
            }
            for path in artifacts.values():
                path.write_text(path.name, encoding="utf-8")

            manifest = JobManifest(root / "manifest.json")
            manager = PipelineStateManager(manifest)
            manifest.ensure_page("page", str(source))
            manager.record_stage_completion(
                "page",
                "preprocessing",
                self._request(
                    "preprocess",
                    source=source,
                    artifacts={"preprocessed_image": artifacts["preprocessing"]},
                    input_artifacts={"source_image": source},
                ),
            )
            manager.record_stage_completion(
                "page",
                "OCR",
                self._request(
                    "ocr",
                    source=source,
                    artifacts={"ocr_result": artifacts["OCR"]},
                    input_artifacts={"preprocessed_image": artifacts["preprocessing"]},
                ),
            )
            manager.record_stage_completion(
                "page",
                "translating",
                self._request(
                    "translation",
                    source=source,
                    artifacts={"translation_result": artifacts["translating"]},
                    input_artifacts={"ocr_result": artifacts["OCR"]},
                ),
            )
            manager.record_stage_completion(
                "page",
                "rendering",
                self._request(
                    "render",
                    source=source,
                    artifacts={"rendered_image": artifacts["rendering"]},
                    input_artifacts={"translation_result": artifacts["translating"]},
                ),
            )
            manager.record_stage_completion(
                "page",
                "export",
                self._request(
                    "export",
                    source=source,
                    artifacts={"archive": artifacts["export"]},
                    input_artifacts={"rendered_image": artifacts["rendering"]},
                ),
            )
            manager.record_stage_completion(
                "page",
                "review",
                self._request(
                    "review",
                    source=source,
                    artifacts={"reviewed_translation": artifacts["review"]},
                    input_artifacts={"rendered_image": artifacts["rendering"]},
                ),
            )

            affected = manager.invalidate_from("page", "translation")

            self.assertEqual(("translating", "rendering", "review", "export"), affected)
            remaining = manifest.pages["page"].stage_records
            self.assertIn("preprocessing", remaining)
            self.assertIn("OCR", remaining)
            self.assertNotIn("translating", remaining)
            self.assertNotIn("rendering", remaining)
            self.assertNotIn("review", remaining)
            self.assertNotIn("export", remaining)

    def test_stage_completion_records_debuggable_metadata(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "page.png"
            source.write_bytes(b"source")
            ocr = root / "ocr.json"
            ocr.write_text("ocr", encoding="utf-8")
            translation = root / "translation.json"
            translation.write_text("translation", encoding="utf-8")

            manifest = JobManifest(root / "manifest.json")
            manager = PipelineStateManager(manifest)
            manifest.ensure_page("page", str(source))
            manager.record_stage_completion(
                "page",
                "translating",
                self._request(
                    "translation-input",
                    source=source,
                    artifacts={"translation_result": translation},
                    input_artifacts={"ocr_result": ocr},
                    provider="groq",
                ),
                duration_ms=813,
                metadata={"provider": "groq"},
            )

            metadata = manifest.pages["page"].stage_records["translating"]["metadata"]
            self.assertEqual("complete", metadata["status"])
            self.assertEqual(__version__, metadata["pipeline_version"])
            self.assertEqual(813, metadata["duration_ms"])
            self.assertEqual("groq", metadata["provider"])
            self.assertEqual({"ocr_result": str(ocr)}, metadata["dependencies"])

    def test_manifest_stage_record_adds_metadata_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "page.png"
            source.write_bytes(b"source")
            ocr = root / "ocr.json"
            ocr.write_text("ocr", encoding="utf-8")
            translation = root / "translation.json"
            translation.write_text("translation", encoding="utf-8")
            manifest = JobManifest(root / "manifest.json")
            manifest.ensure_page("page", str(source))

            manifest.record_stage(
                "page",
                "translating",
                input_fingerprint="translation-input",
                artifacts={"translation_result": translation},
                input_artifacts={"ocr_result": ocr},
                source_path=source,
                application_version=__version__,
            )

            metadata = manifest.pages["page"].stage_records["translating"]["metadata"]
            self.assertEqual("complete", metadata["status"])
            self.assertEqual(__version__, metadata["pipeline_version"])
            self.assertEqual(0, metadata["duration_ms"])
            self.assertEqual({"ocr_result": str(ocr)}, metadata["dependencies"])

    def test_validator_repairs_missing_stage_metadata_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ocr = root / "ocr.json"
            ocr.write_text("ocr", encoding="utf-8")
            translation = root / "translation.json"
            translation.write_text("translation", encoding="utf-8")
            manifest = JobManifest(root / "manifest.json")
            manifest.ensure_page("page", "source.png")
            manifest.pages["page"].stage_records["translating"] = {
                "application_version": __version__,
                "input_artifacts": {
                    "ocr_result": JobManifest.artifact_fingerprint(ocr),
                },
                "output_artifacts": {
                    "translation_result": JobManifest.artifact_fingerprint(
                        translation,
                    ),
                },
                "metadata": {},
            }
            manifest.save()

            repaired = PipelineStateManager(manifest).validator.repair_stage_metadata_defaults()

            metadata = manifest.pages["page"].stage_records["translating"]["metadata"]
            self.assertEqual(1, repaired)
            self.assertEqual("complete", metadata["status"])
            self.assertEqual(__version__, metadata["pipeline_version"])
            self.assertEqual(0, metadata["duration_ms"])
            self.assertEqual({"ocr_result": str(ocr.resolve())}, metadata["dependencies"])

    def test_pipeline_and_workspace_do_not_bypass_state_manager_for_reuse(self):
        root = Path(__file__).resolve().parents[1]
        for relative in (
            "hydra_manga_tl/phase/pipeline.py",
            "hydra_manga_tl/project/workspace.py",
        ):
            source = (root / relative).read_text(encoding="utf-8")
            self.assertNotIn(".stage_reusable(", source, relative)
            self.assertNotIn(".invalidate_from(", source, relative)
            self.assertNotIn("job_manifest.record_stage(", source, relative)


if __name__ == "__main__":
    unittest.main()
