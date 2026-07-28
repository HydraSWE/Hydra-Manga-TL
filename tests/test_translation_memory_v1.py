from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
import json
import os
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if os.name == "nt":
    site_packages = (
        Path(__file__).resolve().parents[1]
        / ".venv"
        / "Lib"
        / "site-packages"
    )
    for dll_dir in (site_packages / "PySide6", site_packages / "shiboken6"):
        if dll_dir.is_dir():
            os.add_dll_directory(str(dll_dir))

from PIL import Image, ImageDraw
from PySide6.QtWidgets import QApplication, QMessageBox

from hydra_manga_tl.project.editor import RegionEdit
from hydra_manga_tl.project.model import ImageRecord, MangaProject
from hydra_manga_tl.project.workspace import WorkspaceManager
from hydra_manga_tl.core.paths import AppPaths
from hydra_manga_tl.core.settings import AppSettings
from hydra_manga_tl.translation.engines import (
    PageDialogue,
    PageTranslation,
    TranslationEngineManager,
)
from hydra_manga_tl.translation.memory import (
    REGION_HASH_PREFIX,
    SCHEMA_VERSION,
    TEXT_HASH_PREFIX,
    TranslationMemory,
    learn_validated_page,
    normalize_tm_source_text,
    source_region_hash,
    source_text_hash,
)


class CountingEngine:
    engine_id = "counting:model-v1"

    def __init__(self) -> None:
        self.calls = 0

    def load(self) -> None:
        return

    def unload(self) -> None:
        return

    def translate_page(self, page: PageDialogue) -> PageTranslation:
        self.calls += 1
        return PageTranslation(
            page.source_language,
            page.target_language,
            [
                {"id": item["id"], "text": f"EN:{item['text']}"}
                for item in page.dialogue
            ],
        )


class TranslationMemoryIdentityTests(unittest.TestCase):
    def test_normalization_is_format_only_and_versioned(self):
        self.assertEqual(
            normalize_tm_source_text("  ありがとう！\r\n  次  "),
            "ありがとう! 次",
        )
        self.assertEqual(source_text_hash("ありがとう！"), source_text_hash("ありがとう!"))
        self.assertNotEqual(source_text_hash("ありがとう！"), source_text_hash("ありがとう"))
        self.assertNotEqual(source_text_hash("Hello"), source_text_hash("hello"))
        self.assertTrue(source_text_hash("text").startswith(TEXT_HASH_PREFIX))

    def test_region_fingerprint_is_stable_versioned_metadata(self):
        image = Image.new("RGB", (80, 80), "white")
        ImageDraw.Draw(image).rectangle((20, 20, 60, 60), fill="black")
        polygons = [[[10, 10], [70, 10], [70, 70], [10, 70]]]
        first = source_region_hash(image, polygons)
        second = source_region_hash(image.copy(), polygons)
        self.assertEqual(first, second)
        self.assertTrue(str(first).startswith(REGION_HASH_PREFIX))
        self.assertIsNone(source_region_hash(image, []))


class TranslationMemoryDatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.memory = TranslationMemory(
            root / "translation_memory.db",
            legacy_path=root / "translation_memory.json",
        )

    def tearDown(self) -> None:
        self.folder.cleanup()

    def record(self, **overrides):
        values = {
            "source_text": "ありがとう！",
            "translated_text": "Thank you!",
            "source_language": "Japanese",
            "target_language": "en",
            "region_type": "dialogue",
            "translation_provider": "groq",
            "provider_model": "model",
        }
        values.update(overrides)
        return self.memory.record(**values)

    def test_schema_index_lookup_usage_and_language_type_boundaries(self):
        entry = self.record()
        self.assertEqual(self.memory.database.schema_version, SCHEMA_VERSION)
        match = self.memory.lookup(
            source_text="ありがとう!",
            source_language="japanese",
            target_language="EN",
            region_type="speech",
        )
        self.assertEqual(match.entry.id, entry.id)
        self.assertEqual(match.entry.usage_count, 1)
        self.assertIsNone(self.memory.lookup(
            source_text="ありがとう!",
            source_language="chinese",
            target_language="en",
            region_type="dialogue",
        ))
        self.assertIsNone(self.memory.lookup(
            source_text="ありがとう!",
            source_language="japanese",
            target_language="fr",
            region_type="dialogue",
        ))
        self.assertIsNone(self.memory.lookup(
            source_text="ありがとう!",
            source_language="japanese",
            target_language="en",
            region_type="title",
        ))
        self.assertTrue(any(
            "ix_tm_exact_lookup" in detail
            for detail in self.memory.database.explain_lookup_plan()
        ))

    def test_hash_collision_still_requires_normalized_text(self):
        entry = self.record(source_text="first")
        with patch(
            "hydra_manga_tl.translation.memory.database.source_text_hash",
            return_value=entry.source_text_hash,
        ):
            self.assertIsNone(self.memory.lookup(
                source_text="collision",
                source_language="japanese",
                target_language="en",
                region_type="dialogue",
                include_legacy=False,
            ))

    def test_priority_and_duplicate_merge_are_deterministic(self):
        provider = self.record(
            translated_text="Provider",
            quality_score=1.0,
        )
        imported = self.record(
            translated_text="Imported",
            verified=True,
            origin="imported",
            quality_score=0.2,
        )
        user = self.memory.record_user_edit(
            source_text="ありがとう！",
            translated_text="User",
            source_language="Japanese",
            target_language="en",
            region_type="dialogue",
            translation_provider="user",
            quality_score=1.0,
        )
        duplicate = self.memory.record_user_edit(
            source_text="ありがとう！",
            translated_text="User",
            source_language="Japanese",
            target_language="en",
            region_type="dialogue",
            source_region_hash=f"{REGION_HASH_PREFIX}{'1' * 16}",
            translation_provider="user",
            quality_score=1.0,
        )
        self.assertEqual(user.id, duplicate.id)
        self.assertNotEqual(provider.id, imported.id)
        match = self.memory.lookup(
            source_text="ありがとう!",
            source_language="Japanese",
            target_language="en",
            region_type="dialogue",
        )
        self.assertEqual(match.translated_text, "User")
        self.assertTrue(match.entry.verified)
        self.assertTrue(match.entry.user_edited)

    def test_region_hash_never_participates_in_lookup(self):
        shared = f"{REGION_HASH_PREFIX}{'a' * 16}"
        self.record(source_text="first", source_region_hash=shared)
        self.assertIsNone(self.memory.lookup(
            source_text="second",
            source_language="Japanese",
            target_language="en",
            region_type="dialogue",
        ))

    def test_repeated_entry_hits_count_every_reused_region(self):
        entry = self.record()
        self.memory.record_entry_hits([entry.id, entry.id, entry.id])
        match = self.memory.lookup(
            source_text="ありがとう!",
            source_language="Japanese",
            target_language="en",
            region_type="dialogue",
            record_usage=False,
        )
        self.assertEqual(match.entry.usage_count, 3)
        self.assertEqual(self.memory.statistics().exact_matches, 3)

    def test_statistics_and_concurrent_duplicate_writes(self):
        def write(_):
            return self.record().id

        with ThreadPoolExecutor(max_workers=8) as executor:
            ids = list(executor.map(write, range(32)))
        self.assertEqual(len(set(ids)), 1)
        self.memory.lookup(
            source_text="ありがとう!",
            source_language="Japanese",
            target_language="en",
            region_type="dialogue",
        )
        self.memory.record_provider_call_saved()
        stats = self.memory.statistics()
        self.assertEqual(stats.total_entries, 1)
        self.assertEqual(stats.exact_matches, 1)
        self.assertEqual(stats.provider_calls_saved, 1)

    def test_json_tmx_and_sqlite_round_trip(self):
        region_hash = f"{REGION_HASH_PREFIX}{'b' * 16}"
        self.record(source_region_hash=region_hash, verified=True)
        root = Path(self.folder.name)
        for suffix in ("json", "tmx", "db"):
            exported = root / f"export.{suffix}"
            self.memory.export(exported)
            imported = TranslationMemory(
                root / f"imported_{suffix}.db",
                legacy_path=root / f"imported_{suffix}.json",
            )
            self.assertEqual(imported.import_file(exported), 1)
            match = imported.lookup(
                source_text="ありがとう!",
                source_language="Japanese",
                target_language="en",
                region_type="dialogue",
            )
            self.assertEqual(match.entry.source_region_hash, region_hash)
            self.assertEqual(match.entry.source_text_hash, source_text_hash("ありがとう!"))

    def test_legacy_json_hit_is_reported_separately(self):
        key = self.memory.legacy_key(
            engine_id="legacy",
            source_language="Japanese",
            target_language="en",
            source_text="待て",
            glossary={},
        )
        self.memory.legacy_path.write_text(
            json.dumps({key: "Wait!"}),
            encoding="utf-8",
        )
        match = self.memory.lookup(
            engine_id="legacy",
            source_text="待て",
            source_language="Japanese",
            target_language="en",
            region_type="dialogue",
            glossary={},
        )
        self.assertEqual(match.source, "legacy-cache")
        self.assertEqual(match.entry.id, None)


class TranslationMemoryPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        root = Path(self.folder.name)
        self.memory = TranslationMemory(
            root / "tm.db",
            legacy_path=root / "legacy.json",
        )
        self.page = PageDialogue(
            "Japanese",
            "en",
            [{
                "id": "r1",
                "text": "待て",
                "region_type": "dialogue",
                "source_region_hash": f"{REGION_HASH_PREFIX}{'c' * 16}",
            }],
        )

    def tearDown(self) -> None:
        self.folder.cleanup()

    def test_provider_result_is_not_learned_until_qa_commit(self):
        engine = CountingEngine()
        manager = TranslationEngineManager(
            preferred_engine="marian",
            translation_memory=self.memory,
        )
        first = manager._translate_with_memory(engine, self.page)
        self.assertEqual(engine.calls, 1)
        self.assertEqual(self.memory.statistics().total_entries, 0)
        learn_validated_page(
            self.page,
            first,
            memory=self.memory,
            valid_ids={"r1"},
            project_id="project",
        )
        second = manager._translate_with_memory(engine, self.page)
        self.assertEqual(engine.calls, 1)
        self.assertEqual(second.translations[0]["translation_source"], "translation-memory")
        self.assertEqual(self.memory.statistics().provider_calls_saved, 1)

    def test_rejected_provider_result_is_not_learned(self):
        engine = CountingEngine()
        manager = TranslationEngineManager(
            preferred_engine="marian",
            translation_memory=self.memory,
        )
        result = manager._translate_with_memory(engine, self.page)
        learn_validated_page(
            self.page,
            result,
            memory=self.memory,
            valid_ids=set(),
        )
        self.assertEqual(self.memory.statistics().total_entries, 0)

    def test_tm_precedes_project_page_cache(self):
        self.memory.record_user_edit(
            source_text="待て",
            translated_text="Verified!",
            source_language="Japanese",
            target_language="en",
            region_type="dialogue",
            translation_provider="user",
        )
        manager = TranslationEngineManager(
            preferred_engine="marian",
            translation_memory=self.memory,
        )
        cached = PageTranslation(
            "Japanese",
            "en",
            [{"id": "r1", "text": "Stale cache"}],
        )
        result = manager.translate_cached_page(self.page, cached)
        self.assertEqual(result.translations[0]["text"], "Verified!")
        self.assertEqual(
            result.translations[0]["translation_source"],
            "translation-memory",
        )

    def test_tm_hits_skip_provider_for_every_supported_region_type(self):
        dialogue = []
        for index, region_type in enumerate(
            ("dialogue", "title", "sfx", "sign", "credit"),
            1,
        ):
            source = f"source-{region_type}"
            self.memory.record(
                source_text=source,
                translated_text=f"translated-{region_type}",
                source_language="Japanese",
                target_language="en",
                region_type=region_type,
                translation_provider="import",
                verified=True,
                origin="imported",
            )
            dialogue.append({
                "id": f"r{index}",
                "text": source,
                "region_type": region_type,
            })
        page = PageDialogue("Japanese", "en", dialogue)
        engine = CountingEngine()
        manager = TranslationEngineManager(
            preferred_engine="marian",
            translation_memory=self.memory,
        )
        result = manager._translate_with_memory(engine, page)
        self.assertEqual(engine.calls, 0)
        self.assertEqual(
            [item["translation_source"] for item in result.translations],
            ["translation-memory"] * len(dialogue),
        )


class TranslationMemoryUiAndEditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_clear_memory_requires_confirmation_and_reports_completion(self):
        from hydra_manga_tl.ui.dialogs import SettingsDialog
        import hydra_manga_tl.ui.dialogs as dialogs

        with tempfile.TemporaryDirectory() as folder:
            memory = TranslationMemory(
                Path(folder) / "tm.db",
                legacy_path=Path(folder) / "legacy.json",
            )
            memory.record(
                source_text="待て",
                translated_text="Wait",
                source_language="Japanese",
                target_language="en",
                region_type="dialogue",
            )
            with patch.object(dialogs, "TRANSLATION_MEMORY", memory):
                dialog = SettingsDialog()
                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.No,
                ):
                    dialog._clear_translation_memory()
                self.assertEqual(memory.statistics().total_entries, 1)
                with (
                    patch.object(
                        QMessageBox,
                        "question",
                        return_value=QMessageBox.StandardButton.Yes,
                    ),
                    patch.object(QMessageBox, "information") as information,
                ):
                    dialog._clear_translation_memory()
                self.assertEqual(memory.statistics().total_entries, 0)
                information.assert_called_once()
                dialog.close()

    def test_translation_memory_settings_round_trip(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = AppPaths(Path(folder))
            settings = AppSettings(
                translation_memory_enabled=False,
                translation_memory_auto_learn=False,
                translation_memory_store_user_edits=False,
                translation_memory_prefer_verified=False,
            )
            settings.save(paths)
            loaded = AppSettings.load(paths)
            self.assertFalse(loaded.translation_memory_enabled)
            self.assertFalse(loaded.translation_memory_auto_learn)
            self.assertFalse(loaded.translation_memory_store_user_edits)
            self.assertFalse(loaded.translation_memory_prefer_verified)

    def test_translation_memory_can_rebind_database_path(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            memory = TranslationMemory(root / "old.db")
            memory.record(
                source_text="待て",
                translated_text="Wait",
                source_language="Japanese",
                target_language="en",
                region_type="dialogue",
            )
            self.assertEqual(memory.statistics().total_entries, 1)
            memory.configure(
                root / "new.db",
                legacy_path=root / "new-legacy.json",
            )
            self.assertEqual(memory.path, root / "new.db")
            self.assertEqual(memory.legacy_path, root / "new-legacy.json")
            self.assertEqual(memory.statistics().total_entries, 0)

    def test_user_edit_is_stored_as_verified(self):
        import hydra_manga_tl.project.workspace as workspace_module

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            image_path = root / "page.png"
            Image.new("RGB", (100, 100), "white").save(image_path)
            project = MangaProject(
                "project",
                "Project",
                str(root),
                source_language="Japanese",
                target_language="en",
                images=[
                    ImageRecord(
                        "image",
                        str(image_path),
                        "page.png",
                        source_language="Japanese",
                    )
                ],
            )
            memory = TranslationMemory(
                root / "tm.db",
                legacy_path=root / "legacy.json",
            )
            manager = SimpleNamespace(current=project)
            group = {
                "original_text": "待て",
                "translated_text": "Wait",
                "bubble_type": "dialogue",
                "source_polygons": [
                    [[10, 10], [90, 10], [90, 90], [10, 90]]
                ],
            }
            edit = RegionEdit(translated_text="Stop!")
            with (
                patch.object(workspace_module, "TRANSLATION_MEMORY", memory),
                patch.object(
                    workspace_module.SETTINGS,
                    "translation_memory_enabled",
                    True,
                ),
                patch.object(
                    workspace_module.SETTINGS,
                    "translation_memory_store_user_edits",
                    True,
                ),
            ):
                WorkspaceManager._learn_user_translation_edit(
                    manager,
                    0,
                    group,
                    edit,
                )
            match = memory.lookup(
                source_text="待て",
                source_language="Japanese",
                target_language="en",
                region_type="dialogue",
            )
            self.assertEqual(match.translated_text, "Stop!")
            self.assertTrue(match.entry.verified)
            self.assertTrue(match.entry.user_edited)


if __name__ == "__main__":
    unittest.main()
