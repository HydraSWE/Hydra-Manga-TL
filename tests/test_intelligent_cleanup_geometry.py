from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from hydra_manga_tl.core.fonts import default_font_file
from hydra_manga_tl.phase.layout import TextGroup
from hydra_manga_tl.phase.phase3 import (
    _render_text_color,
    _surface_report,
    _title_render_group,
    placement_candidates,
    renderer_for_region,
    run as render_phase3,
)
from hydra_manga_tl.phase.pipeline import _text_group_without_preserved_marks
from hydra_manga_tl.phase.renderer import (
    apply_manual_backgrounds,
    clean_background,
    fit_text_avoiding_preserved,
    make_mask,
    preserved_constraint_mask,
    render_group,
)
from hydra_manga_tl.phase.renderer_text import FittedText
from hydra_manga_tl.phase.segmentation import _fallback_surface, segment_bubble
from hydra_manga_tl.title.reconstruction.models import (
    ProviderCapabilities,
    TitleReconstructionReport,
    TitleReconstructionResult,
)
from hydra_manga_tl.project.migrations.manager import MigrationManager
from hydra_manga_tl.project.manual_region import split_manual_source_regions
from hydra_manga_tl.project.model import ManualRegion, PROJECT_VERSION
from hydra_manga_tl.project.workspace_editor import transform_background_polygon


SELECTION = [[5, 5], [115, 5], [115, 75], [5, 75]]
CLEANUP = [[15, 15], [30, 15], [30, 30], [15, 30]]
PLACEMENT = [[40, 10], [110, 10], [110, 70], [40, 70]]
HEART = [[34, 35], [48, 35], [48, 49], [34, 49]]
CENTER_HEART = [[58, 34], [82, 34], [82, 62], [58, 62]]


class IntelligentCleanupGeometryTests(unittest.TestCase):
    def test_linked_background_follows_text_translation_and_resize(self) -> None:
        polygon = [[10, 10], [50, 10], [50, 30], [10, 30]]

        transformed = transform_background_polygon(
            polygon,
            {"x": 10, "y": 10, "width": 40, "height": 20, "angle": 0},
            {"x": 20, "y": 5, "width": 80, "height": 40, "angle": 0},
        )

        self.assertEqual(transformed, [[20, 5], [100, 5], [100, 45], [20, 45]])

    def test_linked_background_follows_text_rotation(self) -> None:
        polygon = [[10, 10], [50, 10], [50, 30], [10, 30]]

        transformed = transform_background_polygon(
            polygon,
            {"x": 10, "y": 10, "width": 40, "height": 20, "angle": 0},
            {"x": 10, "y": 10, "width": 40, "height": 20, "angle": 90},
        )

        self.assertEqual(transformed, [[40, 0], [40, 40], [20, 40], [20, 0]])

    def test_manual_background_uses_its_polygon_without_affecting_automatic_groups(self) -> None:
        image = Image.new("RGB", (140, 100), (30, 30, 30))
        polygon = [[20, 10], [100, 20], [85, 80], [30, 70]]

        count = apply_manual_backgrounds(image, [
            {"manual": True, "background_polygon": polygon},
            {"manual": False, "background_polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]},
            {"manual": True, "bubble_type": "title", "background_polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]},
        ])

        self.assertEqual(count, 1)
        self.assertEqual(image.getpixel((50, 40)), (255, 255, 255))
        self.assertEqual(image.getpixel((5, 5)), (30, 30, 30))

    def test_light_dialogue_render_has_no_white_halo(self) -> None:
        image = Image.new("RGB", (120, 80), "white")
        rendered = render_group(
            image,
            FittedText(["Hello"], 20, [10, 10, 110, 70], 24),
            "Hello",
            default_font_file(),
        )

        self.assertEqual(rendered["text_color"], "#0a0a0a")
        self.assertEqual(rendered["stroke_width"], 0)
        self.assertIsNone(rendered["stroke_color"])

    def test_dark_dialogue_render_keeps_contrast_stroke(self) -> None:
        image = Image.new("RGB", (120, 80), "black")
        rendered = render_group(
            image,
            FittedText(["Hello"], 20, [10, 10, 110, 70], 24),
            "Hello",
            default_font_file(),
        )

        self.assertEqual(rendered["text_color"], "#ffffff")
        self.assertGreater(rendered["stroke_width"], 0)
        self.assertEqual(rendered["stroke_color"], "#000000")

    def test_cleanup_classifies_light_and_dark_regions_independently(self) -> None:
        image = np.full((80, 160, 3), 245, dtype=np.uint8)
        image[:, 80:] = 10
        mask = np.zeros((80, 160), dtype=np.uint8)
        mask[25:55, 25:55] = 255
        mask[25:55, 105:135] = 255

        cleaned, method = clean_background(image, mask)

        self.assertGreater(float(cleaned[30:50, 30:50].mean()), 220)
        self.assertLess(float(cleaned[30:50, 110:130].mean()), 35)
        self.assertIn("light-background-fill", method)
        self.assertIn("dark-background-fill", method)

    def test_cleanup_does_not_flatten_mixed_manga_art_to_white(self) -> None:
        image = np.full((120, 160, 3), 245, dtype=np.uint8)
        image[:, 80:] = 25
        mask = np.zeros((120, 160), dtype=np.uint8)
        mask[10:110, 55:105] = 255

        cleaned, method = clean_background(image, mask)

        self.assertEqual(method, "opencv-inpaint")
        self.assertLess(float(cleaned[20:100, 90:100].mean()), 200)

    def test_dark_caption_segmentation_records_dark_surface(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            image = Image.new("RGB", (180, 120), "white")
            pixels = np.asarray(image).copy()
            pixels[15:105, 35:145] = 8
            image = Image.fromarray(pixels)
            result = segment_bubble(
                image,
                [[55, 30], [125, 30], [125, 90], [55, 90]],
                mask_path=Path(folder) / "mask.png",
            )

        self.assertEqual(result.method, "dark_component")
        self.assertEqual(result.mask_quality["surface"], "dark")

    def test_surface_metadata_controls_contrast_without_overriding_manual_color(self) -> None:
        dark = {"bubble_segmentation": {"mask_quality": {"surface": "dark"}}}
        light = {"bubble_segmentation": {"mask_quality": {"surface": "light"}}}

        self.assertEqual(_render_text_color(dark), "#ffffff")
        self.assertEqual(_render_text_color(light), "#0a0a0a")
        self.assertEqual(_render_text_color({**dark, "text_color": "#123456"}), "#123456")

    def test_dialogue_surface_never_selects_title_typography(self) -> None:
        for surface in ("light", "dark", "art", "unknown"):
            with self.subTest(surface=surface):
                group = {
                    "type": "dialogue",
                    "bubble_type": "dialogue",
                    "bubble_segmentation": {
                        "failure_reason": "low_mask_confidence",
                        "mask_quality": {"surface": surface},
                    },
                }
                self.assertEqual(renderer_for_region(group), "dialogue")
                confidence, review_reason = _surface_report(group)
                self.assertLessEqual(confidence, 0.69)
                self.assertEqual(review_reason, "surface_fallback:low_mask_confidence")

    def test_manual_art_text_override_remains_authoritative(self) -> None:
        group = {
            "type": "dialogue",
            "bubble_type": "dialogue",
            "manual": True,
            "render_mode": "art_text",
        }

        self.assertEqual(renderer_for_region(group), "title")

    def test_fallback_surface_prioritizes_bright_bubble_over_text_edges(self) -> None:
        pixels = np.full((100, 120, 3), 245, dtype=np.uint8)
        pixels[8:92, 8:11] = 0
        pixels[8:92, 109:112] = 0
        pixels[8:11, 8:112] = 0
        pixels[89:92, 8:112] = 0
        for x in range(25, 96, 12):
            pixels[22:78, x:x + 3] = 0

        result = _fallback_surface(Image.fromarray(pixels), [[8, 8], [112, 8], [112, 92], [8, 92]])

        self.assertEqual(result["surface"], "light")
        self.assertGreater(result["surface_bright_fraction"], 0.65)

    def test_fallback_surface_recognizes_uniform_dark_caption(self) -> None:
        pixels = np.full((100, 120, 3), 12, dtype=np.uint8)
        for x in range(30, 91, 15):
            pixels[22:78, x:x + 2] = 245

        result = _fallback_surface(Image.fromarray(pixels), [[8, 8], [112, 8], [112, 92], [8, 92]])

        self.assertEqual(result["surface"], "dark")
        self.assertGreater(result["surface_dark_fraction"], 0.65)

    def test_fallback_surface_keeps_varied_background_as_art(self) -> None:
        values = np.indices((100, 120)).sum(axis=0) % 2
        pixels = np.repeat(np.where(values[..., None] == 0, 35, 220).astype(np.uint8), 3, axis=2)

        result = _fallback_surface(Image.fromarray(pixels), [[8, 8], [112, 8], [112, 92], [8, 92]])

        self.assertEqual(result["surface"], "art")

    def test_rejected_art_dialogue_uses_conservative_mask_without_background_plate(self) -> None:
        from hydra_manga_tl.title.reconstruction.engine import reconstruct_title_page

        image = Image.new("RGB", (120, 80), (150, 150, 150))
        group = {
            "index": 1,
            "art_dialogue": True,
            "art_cleanup_fallback": "outlined-review",
            "cleanup_polygons": [[[35, 15], [55, 15], [55, 65], [35, 65]]],
            "source_polygons": [[[35, 15], [55, 15], [55, 65], [35, 65]]],
        }
        rejected = TitleReconstructionResult(
            provider="opencv",
            capabilities=ProviderCapabilities(),
        )
        report = TitleReconstructionReport(
            group=1,
            provider="opencv",
            capabilities=ProviderCapabilities(),
            status="review",
            confidence=0.0,
            mask_quality=0.0,
            background_quality=0.0,
            cleanup_quality=0.0,
            cleanup_method="",
            warning="mask_not_safe",
        )

        with tempfile.TemporaryDirectory() as folder, patch(
            "hydra_manga_tl.title.reconstruction.engine.reconstruct_title_group",
            return_value=(rejected, report),
        ):
            result = reconstruct_title_page(
                image,
                [group],
                np.zeros((80, 120), dtype=np.uint8),
                output_dir=Path(folder),
                stem="page",
                configured_inpaint_python=str(Path(folder) / "missing.exe"),
            )

        self.assertGreater(int(np.count_nonzero(result.mask)), 0)
        self.assertEqual(result.background_plate_reports, [])
        self.assertTrue(result.needs_review)
        self.assertIn("opencv-inpaint", result.cleaning_method)
        self.assertEqual(group["title_reconstruction_report"]["title_reconstruction_status"], "review")
        self.assertEqual(group["title_reconstruction_report"]["title_reconstruction_warning"], "art_dialogue_conservative_mask")

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
        self.assertEqual(decorative_symbols[0]["render_policy"], "preserve_original")
        self.assertEqual(decorative_symbols[0]["preserve_policy"], "preserve_original")

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
        self.assertEqual(decorative_symbols[0]["render_policy"], "preserve_original")

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
        self.assertEqual(int(mask[40, 40]), 0)
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

    def test_make_mask_preserves_isolated_decorative_symbol(self) -> None:
        mask = make_mask(
            (140, 100),
            [{
                "cleanup_polygons": [CLEANUP, HEART],
                "decorative_symbols": [{
                    "polygon": HEART,
                    "render_policy": "preserve_original",
                }],
            }],
            dilation=0,
        )

        self.assertEqual(int(mask[20, 20]), 255)
        self.assertEqual(int(mask[45, 45]), 0)

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
            self.assertEqual(region["background_polygon"], SELECTION)
            self.assertTrue(region["background_linked"])
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
        self.assertIn("surface_classification", rendered)
        self.assertIn("surface_confidence", rendered)
        self.assertIn("surface_review_reason", rendered)
        self.assertIn("cleanup_method", rendered)
        self.assertIn("overflow", rendered)
        self.assertIn("text_color", rendered)
        self.assertIn("stroke_color", rendered)
        self.assertIn(
            rendered["constraint_strategy"],
            {"preserved_content_aware", "preserved_content_distributed"},
        )


if __name__ == "__main__":
    unittest.main()
