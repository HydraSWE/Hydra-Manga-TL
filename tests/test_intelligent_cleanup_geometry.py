from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from hydra_manga_tl.core.fonts import default_font_file
from hydra_manga_tl.phase.layout import TextGroup
from hydra_manga_tl.phase.phase3 import placement_candidates, run as render_phase3
from hydra_manga_tl.phase.pipeline import _text_group_without_preserved_marks
from hydra_manga_tl.phase.renderer import (
    fit_text_avoiding_preserved,
    make_mask,
    preserved_constraint_mask,
)
from hydra_manga_tl.project.migrations.manager import MigrationManager
from hydra_manga_tl.project.manual_region import split_manual_source_regions
from hydra_manga_tl.project.model import ManualRegion, PROJECT_VERSION


SELECTION = [[5, 5], [115, 5], [115, 75], [5, 75]]
CLEANUP = [[15, 15], [30, 15], [30, 30], [15, 30]]
PLACEMENT = [[40, 10], [110, 10], [110, 70], [40, 70]]
HEART = [[34, 35], [48, 35], [48, 49], [34, 49]]
CENTER_HEART = [[58, 34], [82, 34], [82, 62], [58, 62]]


class IntelligentCleanupGeometryTests(unittest.TestCase):
    def test_manual_region_keeps_selection_cleanup_and_placement_separate(self) -> None:
        region = ManualRegion(
            id="manual-1",
            rect=[5, 5, 115, 75],
            source_polygons=[SELECTION],
            selection_polygon=SELECTION,
            cleanup_polygons=[CLEANUP],
            placement_polygon=PLACEMENT,
            original_text="乳首",
            translated_text="nipple",
            ocr_confidence=0.95,
            source_language="Japanese",
            direction="vertical-rtl",
            status="translated",
        )

        self.assertEqual(region.polygon, SELECTION)
        self.assertEqual(region.selection_polygon, SELECTION)
        self.assertEqual(region.cleanup_polygons, [CLEANUP])
        self.assertEqual(region.placement_polygon, PLACEMENT)

    def test_manual_source_split_extracts_redrawable_decorative_symbols(self) -> None:
        text_regions, decorative_symbols = split_manual_source_regions([
            {"text": "あ", "confidence": 0.94, "polygon": CLEANUP},
            {"text": "♥", "confidence": 0.88, "polygon": HEART},
        ])

        self.assertEqual([region["text"] for region in text_regions], ["あ"])
        self.assertEqual(len(decorative_symbols), 1)
        self.assertEqual(decorative_symbols[0]["text"], "♥")
        self.assertEqual(decorative_symbols[0]["polygon"], HEART)
        self.assertEqual(decorative_symbols[0]["render_policy"], "redraw_semantic")
        self.assertNotIn("preserve_policy", decorative_symbols[0])

    def test_auto_group_split_removes_marks_from_translation_text(self) -> None:
        group, member_indices, decorative_symbols = _text_group_without_preserved_marks(
            TextGroup([1, 2], "あ♥", [15, 15, 48, 49], "vertical-rtl"),
            [
                {"text": "あ", "confidence": 0.94, "polygon": CLEANUP},
                {"text": "♥", "confidence": 0.88, "polygon": HEART},
            ],
        )

        self.assertEqual(group.text, "あ")
        self.assertEqual(member_indices, [1])
        self.assertEqual(len(decorative_symbols), 1)
        self.assertEqual(decorative_symbols[0]["render_policy"], "redraw_semantic")

    def test_make_mask_prefers_cleanup_polygons_over_legacy_source_polygons(self) -> None:
        mask = make_mask(
            (140, 100),
            [{
                "source_polygons": [SELECTION],
                "cleanup_polygons": [CLEANUP],
            }],
            dilation=0,
        )

        self.assertEqual(int(mask[20, 20]), 255)
        self.assertEqual(int(mask[60, 100]), 0)

    def test_make_mask_excludes_preserved_mark_polygons(self) -> None:
        mask = make_mask(
            (140, 100),
            [{
                "cleanup_polygons": [SELECTION],
                "preserved_marks": [{
                    "polygon": HEART,
                    "preserve_policy": "preserve_original",
                }],
            }],
            dilation=0,
        )

        self.assertEqual(int(mask[20, 20]), 255)
        self.assertEqual(int(mask[40, 40]), 0)

    def test_make_mask_cleans_redrawable_decorative_symbols(self) -> None:
        mask = make_mask(
            (140, 100),
            [{
                "cleanup_polygons": [CLEANUP, HEART],
                "decorative_symbols": [{
                    "polygon": HEART,
                    "render_policy": "redraw_semantic",
                }],
                "preserved_marks": [],
            }],
            dilation=0,
        )

        self.assertEqual(int(mask[20, 20]), 255)
        self.assertEqual(int(mask[40, 40]), 255)

    def test_make_mask_clips_cleanup_to_selection_polygon_after_dilation(self) -> None:
        tapered = [[20, 0], [120, 0], [82, 100], [45, 100]]
        mask = make_mask(
            (140, 100),
            [{
                "cleanup_polygons": [[[15, 78], [95, 78], [95, 98], [15, 98]]],
                "selection_polygon": tapered,
            }],
            dilation=4,
        )

        self.assertEqual(int(mask[90, 20]), 0)
        self.assertGreater(int(mask[88, 70]), 0)

    def test_preserved_constraint_mask_blocks_mark_area_only(self) -> None:
        mask = preserved_constraint_mask(
            (140, 100),
            {
                "preserved_marks": [{
                    "polygon": CENTER_HEART,
                    "preserve_policy": "preserve_original",
                }],
            },
            [0, 0, 140, 100],
            margin=0,
        )

        self.assertEqual(int(mask[44, 70]), 255)
        self.assertEqual(int(mask[20, 20]), 0)

    def test_preserved_constraint_mask_blocks_outside_placement_polygon(self) -> None:
        tapered = [[20, 0], [120, 0], [82, 100], [45, 100]]
        mask = preserved_constraint_mask(
            (140, 100),
            {"placement_polygon": tapered},
            [0, 0, 140, 100],
            margin=0,
        )

        self.assertEqual(int(mask[90, 20]), 255)
        self.assertEqual(int(mask[50, 65]), 0)

    def test_constrained_fit_places_text_around_preserved_mark(self) -> None:
        fitted = fit_text_avoiding_preserved(
            "Ah",
            [0, 0, 140, 100],
            default_font_file(),
            {
                "preserved_marks": [{
                    "polygon": CENTER_HEART,
                    "preserve_policy": "preserve_original",
                }],
            },
            (140, 100),
            maximum=28,
            minimum=10,
        )

        self.assertIsNotNone(fitted)
        assert fitted is not None
        self.assertTrue(fitted.preserved_content_aware)
        self.assertIn(
            fitted.constraint_strategy,
            {"preserved_content_aware", "preserved_content_distributed"},
        )
        self.assertIsNotNone(fitted.line_positions)
        self.assertNotEqual(fitted.line_positions, [])
        self.assertTrue(
            fitted.line_positions[0][0] + 5 < CENTER_HEART[0][0]
            or fitted.line_positions[0][0] > CENTER_HEART[1][0]
        )

    def test_constrained_fit_ignores_redrawable_decorative_symbols(self) -> None:
        fitted = fit_text_avoiding_preserved(
            "Ah",
            [0, 0, 140, 100],
            default_font_file(),
            {
                "decorative_symbols": [{
                    "polygon": CENTER_HEART,
                    "render_policy": "redraw_semantic",
                }],
            },
            (140, 100),
            maximum=28,
            minimum=10,
        )

        self.assertIsNone(fitted)

    def test_manual_exact_placement_uses_placement_polygon_not_cleanup_polygon(self) -> None:
        candidates = placement_candidates(
            {
                "manual": True,
                "placement_policy": "exact",
                "manual_rect": [5, 5, 115, 75],
                "polygon": SELECTION,
                "cleanup_polygons": [CLEANUP],
                "placement_polygon": PLACEMENT,
            },
            (140, 100),
        )

        self.assertEqual(candidates[0], ("manual_exact_bounds", [40, 10, 110, 70]))

    def test_schema_8_migration_adds_geometry_to_active_and_target_manual_regions(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project_file = root / "project.json"
            manual = {
                "id": "manual-1",
                "rect": [5, 5, 115, 75],
                "source_polygons": [SELECTION],
                "polygon": SELECTION,
                "original_text": "乳首",
                "translated_text": "nipple",
                "ocr_confidence": 0.9,
                "source_language": "Japanese",
                "direction": "vertical-rtl",
                "status": "translated",
            }
            payload = {
                "id": "project-1",
                "name": "Chapter",
                "root": str(root),
                "version": 8,
                "project_schema": 8,
                "minimum_supported_schema": 8,
                "images": [{
                    "id": "page-1",
                    "source_path": "page.png",
                    "relative_path": "page.png",
                    "manual_regions": [dict(manual)],
                    "target_states": {
                        "en": {"manual_regions": [dict(manual)]},
                    },
                }],
            }
            project_file.write_text(json.dumps(payload), encoding="utf-8")

            MigrationManager().migrate(project_file, target_schema=PROJECT_VERSION)
            migrated = json.loads(project_file.read_text(encoding="utf-8"))

        active = migrated["images"][0]["manual_regions"][0]
        target = migrated["images"][0]["target_states"]["en"]["manual_regions"][0]
        self.assertEqual(migrated["project_schema"], PROJECT_VERSION)
        for region in (active, target):
            self.assertEqual(region["selection_polygon"], SELECTION)
            self.assertEqual(region["cleanup_polygons"], [SELECTION])
            self.assertEqual(region["placement_polygon"], SELECTION)
            self.assertEqual(region["decorative_symbols"], [])
            self.assertEqual(region["preserved_marks"], [])

    def test_phase3_masks_only_cleanup_polygon_for_large_manual_selection(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "page.png"
            Image.new("RGB", (140, 100), (235, 235, 235)).save(source)
            payload = {
                "source": str(source),
                "source_language": "Japanese",
                "target_language": "en",
                "translation_groups": [{
                    "index": "manual-1",
                    "manual": True,
                    "manual_rect": [5, 5, 115, 75],
                    "polygon": SELECTION,
                    "selection_polygon": SELECTION,
                    "cleanup_polygons": [CLEANUP],
                    "placement_polygon": PLACEMENT,
                    "preserved_marks": [{
                        "polygon": HEART,
                        "preserve_policy": "preserve_original",
                    }],
                    "source_polygons": [SELECTION],
                    "original_text": "乳首",
                    "translated_text": "nipple",
                    "source_language": "Japanese",
                    "status": "translated",
                    "direction": "vertical-rtl",
                    "render_direction": "horizontal-ltr",
                    "bubble_type": "dialogue",
                    "type": "dialogue",
                }],
            }
            input_path = root / "page_translated_en.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            output = root / "rendered"

            render_phase3(input_path, output)
            mask = Image.open(output / "page_mask.png").convert("L")

        self.assertGreater(mask.getpixel((20, 20)), 0)
        self.assertEqual(mask.getpixel((40, 40)), 0)
        self.assertEqual(mask.getpixel((100, 60)), 0)

    def test_phase3_reports_preserved_content_aware_layout(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "page.png"
            Image.new("RGB", (140, 100), (235, 235, 235)).save(source)
            payload = {
                "source": str(source),
                "source_language": "Japanese",
                "target_language": "en",
                "translation_groups": [{
                    "index": "manual-1",
                    "manual": True,
                    "manual_rect": [0, 0, 140, 100],
                    "polygon": [[0, 0], [140, 0], [140, 100], [0, 100]],
                    "selection_polygon": [[0, 0], [140, 0], [140, 100], [0, 100]],
                    "cleanup_polygons": [CLEANUP],
                    "placement_polygon": [[0, 0], [140, 0], [140, 100], [0, 100]],
                    "preserved_marks": [{
                        "polygon": CENTER_HEART,
                        "preserve_policy": "preserve_original",
                    }],
                    "source_polygons": [CLEANUP],
                    "original_text": "あ",
                    "translated_text": "Ah",
                    "source_language": "Japanese",
                    "status": "translated",
                    "direction": "vertical-rtl",
                    "render_direction": "horizontal-ltr",
                    "bubble_type": "dialogue",
                    "type": "dialogue",
                }],
            }
            input_path = root / "page_translated_en.json"
            input_path.write_text(json.dumps(payload), encoding="utf-8")
            output = root / "rendered"

            render_phase3(input_path, output)
            report = json.loads((output / "page_render.json").read_text(encoding="utf-8"))

        rendered = report["rendered_groups"][0]
        self.assertTrue(rendered["preserved_content_aware"])
        self.assertIn(
            rendered["constraint_strategy"],
            {"preserved_content_aware", "preserved_content_distributed"},
        )


if __name__ == "__main__":
    unittest.main()
