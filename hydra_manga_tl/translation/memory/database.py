"""SQLite-backed, provider-independent Translation Memory."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
from contextlib import closing, contextmanager
import json
from pathlib import Path
import shutil
import sqlite3
import threading
from typing import Any, Iterable
import xml.etree.ElementTree as ET

from hydra_manga_tl.core.paths import PATHS
from hydra_manga_tl.core.region_types import normalize_region_type

from .fingerprints import normalize_tm_source_text, source_text_hash
from .models import (
    TranslationMemoryEntry,
    TranslationMemoryMatch,
    TranslationMemoryStatistics,
)


SCHEMA_VERSION = 1
_ORIGIN_PRIORITY = {"provider": 1, "imported": 2, "user": 3}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _language_key(value: str) -> str:
    return str(value or "").strip().casefold()


def _entry_from_row(row: sqlite3.Row) -> TranslationMemoryEntry:
    values = dict(row)
    values["verified"] = bool(values.get("verified"))
    values["user_edited"] = bool(values.get("user_edited"))
    return TranslationMemoryEntry(**{
        field.name: values.get(field.name)
        for field in fields(TranslationMemoryEntry)
    })


class TranslationMemoryDatabase:
    """Own SQLite schema, indexed lookup, and thread-safe transactions."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or PATHS.translation_memory)
        self._schema_lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._schema_lock:
            with self._connection() as connection:
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = NORMAL")
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS tm_entries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_text TEXT NOT NULL,
                        normalized_text TEXT NOT NULL,
                        source_text_hash TEXT NOT NULL,
                        source_region_hash TEXT,
                        translated_text TEXT NOT NULL,
                        source_language TEXT NOT NULL,
                        target_language TEXT NOT NULL,
                        region_type TEXT NOT NULL,
                        translation_provider TEXT NOT NULL DEFAULT '',
                        provider_model TEXT NOT NULL DEFAULT '',
                        created_at TEXT NOT NULL,
                        last_used_at TEXT NOT NULL,
                        usage_count INTEGER NOT NULL DEFAULT 0,
                        verified INTEGER NOT NULL DEFAULT 0,
                        user_edited INTEGER NOT NULL DEFAULT 0,
                        quality_score REAL NOT NULL DEFAULT 0.0,
                        origin TEXT NOT NULL DEFAULT 'provider',
                        series_id TEXT,
                        glossary_version TEXT,
                        project_id TEXT,
                        notes TEXT
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_tm_entry_candidate
                    ON tm_entries (
                        source_text_hash, normalized_text, source_language,
                        target_language, region_type, translated_text
                    );
                    CREATE INDEX IF NOT EXISTS ix_tm_exact_lookup
                    ON tm_entries (
                        source_text_hash, source_language, target_language,
                        region_type, normalized_text
                    );
                    CREATE INDEX IF NOT EXISTS ix_tm_region_hash
                    ON tm_entries (source_region_hash)
                    WHERE source_region_hash IS NOT NULL;
                    CREATE INDEX IF NOT EXISTS ix_tm_series_type
                    ON tm_entries (series_id, region_type);
                    CREATE TABLE IF NOT EXISTS tm_metrics (
                        key TEXT PRIMARY KEY,
                        value REAL NOT NULL DEFAULT 0.0
                    );
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @property
    def schema_version(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("PRAGMA user_version").fetchone()[0])

    def lookup(
        self,
        *,
        source_text: str,
        source_language: str,
        target_language: str,
        region_type: str,
        prefer_verified: bool = True,
        record_usage: bool = True,
    ) -> TranslationMemoryEntry | None:
        normalized = normalize_tm_source_text(source_text)
        if not normalized:
            return None
        identity = source_text_hash(normalized)
        priority = (
            """
            CASE
                WHEN verified = 1 AND user_edited = 1 THEN 4
                WHEN verified = 1 AND origin = 'imported' THEN 3
                WHEN verified = 1 THEN 2
                ELSE 1
            END DESC,
            """
            if prefer_verified
            else ""
        )
        query = f"""
            SELECT * FROM tm_entries
            WHERE source_text_hash = ?
              AND source_language = ?
              AND target_language = ?
              AND region_type = ?
              AND normalized_text = ?
            ORDER BY
              {priority}
              quality_score DESC,
              created_at DESC,
              id DESC
            LIMIT 1
        """
        with self._connection() as connection:
            row = connection.execute(
                query,
                (
                    identity,
                    _language_key(source_language),
                    _language_key(target_language),
                    normalize_region_type(region_type),
                    normalized,
                ),
            ).fetchone()
            if row is None:
                return None
            if record_usage:
                now = _utc_now()
                connection.execute(
                    """
                    UPDATE tm_entries
                    SET usage_count = usage_count + 1, last_used_at = ?
                    WHERE id = ?
                    """,
                    (now, int(row["id"])),
                )
                self._increment_metric(connection, "exact_matches", 1.0)
                row = connection.execute(
                    "SELECT * FROM tm_entries WHERE id = ?",
                    (int(row["id"]),),
                ).fetchone()
            return _entry_from_row(row)

    def record_provider_call_saved(
        self,
        *,
        estimated_api_cost: float = 0.0,
        estimated_time_seconds: float = 0.0,
    ) -> None:
        with self._connection() as connection:
            self._increment_metric(connection, "provider_calls_saved", 1.0)
            if estimated_api_cost:
                self._increment_metric(
                    connection,
                    "estimated_api_cost_saved",
                    estimated_api_cost,
                )
            if estimated_time_seconds:
                self._increment_metric(
                    connection,
                    "estimated_time_saved_seconds",
                    estimated_time_seconds,
                )

    def record_entry_hits(self, entry_ids: Iterable[int]) -> None:
        hit_counts: dict[int, int] = {}
        for value in entry_ids:
            entry_id = int(value)
            if entry_id > 0:
                hit_counts[entry_id] = hit_counts.get(entry_id, 0) + 1
        if not hit_counts:
            return
        now = _utc_now()
        with self._connection() as connection:
            connection.executemany(
                """
                UPDATE tm_entries
                SET usage_count = usage_count + ?, last_used_at = ?
                WHERE id = ?
                """,
                [
                    (count, now, entry_id)
                    for entry_id, count in sorted(hit_counts.items())
                ],
            )
            self._increment_metric(
                connection,
                "exact_matches",
                float(sum(hit_counts.values())),
            )

    @staticmethod
    def _increment_metric(
        connection: sqlite3.Connection,
        key: str,
        amount: float,
    ) -> None:
        connection.execute(
            """
            INSERT INTO tm_metrics (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
            """,
            (key, float(amount)),
        )

    def record(
        self,
        *,
        source_text: str,
        translated_text: str,
        source_language: str,
        target_language: str,
        region_type: str = "dialogue",
        source_region_hash: str | None = None,
        translation_provider: str = "",
        provider_model: str = "",
        verified: bool = False,
        user_edited: bool = False,
        quality_score: float = 0.0,
        origin: str = "provider",
        series_id: str | None = None,
        glossary_version: str | None = None,
        project_id: str | None = None,
        notes: str | None = None,
    ) -> TranslationMemoryEntry | None:
        source = str(source_text).strip()
        translated = str(translated_text).strip()
        normalized = normalize_tm_source_text(source)
        if not normalized or not translated:
            return None
        now = _utc_now()
        normalized_origin = origin if origin in _ORIGIN_PRIORITY else "provider"
        values = (
            source,
            normalized,
            source_text_hash(normalized),
            source_region_hash or None,
            translated,
            _language_key(source_language),
            _language_key(target_language),
            normalize_region_type(region_type),
            str(translation_provider or "").strip(),
            str(provider_model or "").strip(),
            now,
            now,
            int(bool(verified)),
            int(bool(user_edited)),
            max(0.0, min(1.0, float(quality_score))),
            normalized_origin,
            series_id,
            glossary_version,
            project_id,
            notes,
        )
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO tm_entries (
                    source_text, normalized_text, source_text_hash,
                    source_region_hash, translated_text, source_language,
                    target_language, region_type, translation_provider,
                    provider_model, created_at, last_used_at, verified,
                    user_edited, quality_score, origin, series_id,
                    glossary_version, project_id, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (
                    source_text_hash, normalized_text, source_language,
                    target_language, region_type, translated_text
                ) DO UPDATE SET
                    source_text = excluded.source_text,
                    source_region_hash = COALESCE(
                        excluded.source_region_hash,
                        tm_entries.source_region_hash
                    ),
                    translation_provider = CASE
                        WHEN excluded.translation_provider != ''
                        THEN excluded.translation_provider
                        ELSE tm_entries.translation_provider
                    END,
                    provider_model = CASE
                        WHEN excluded.provider_model != ''
                        THEN excluded.provider_model
                        ELSE tm_entries.provider_model
                    END,
                    verified = MAX(tm_entries.verified, excluded.verified),
                    user_edited = MAX(tm_entries.user_edited, excluded.user_edited),
                    quality_score = MAX(
                        tm_entries.quality_score,
                        excluded.quality_score
                    ),
                    origin = CASE
                        WHEN excluded.user_edited = 1 THEN 'user'
                        WHEN tm_entries.origin = 'user' THEN tm_entries.origin
                        WHEN excluded.origin = 'imported' THEN 'imported'
                        ELSE tm_entries.origin
                    END,
                    series_id = COALESCE(excluded.series_id, tm_entries.series_id),
                    glossary_version = COALESCE(
                        excluded.glossary_version,
                        tm_entries.glossary_version
                    ),
                    project_id = COALESCE(excluded.project_id, tm_entries.project_id),
                    notes = COALESCE(excluded.notes, tm_entries.notes)
                """,
                values,
            )
            row = connection.execute(
                """
                SELECT * FROM tm_entries
                WHERE source_text_hash = ?
                  AND normalized_text = ?
                  AND source_language = ?
                  AND target_language = ?
                  AND region_type = ?
                  AND translated_text = ?
                """,
                (
                    values[2], normalized, values[5], values[6], values[7], translated,
                ),
            ).fetchone()
            return _entry_from_row(row) if row is not None else None

    def all_entries(self) -> list[TranslationMemoryEntry]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tm_entries ORDER BY created_at, id"
            ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def statistics(self) -> TranslationMemoryStatistics:
        with self._connection() as connection:
            counts = connection.execute(
                """
                SELECT
                    COUNT(*) AS total_entries,
                    SUM(CASE WHEN verified = 1 THEN 1 ELSE 0 END) AS verified_entries,
                    SUM(CASE WHEN user_edited = 1 THEN 1 ELSE 0 END) AS user_edited_entries,
                    SUM(CASE WHEN origin = 'imported' THEN 1 ELSE 0 END) AS imported_entries
                FROM tm_entries
                """
            ).fetchone()
            metrics = {
                str(row["key"]): float(row["value"])
                for row in connection.execute("SELECT key, value FROM tm_metrics")
            }
        return TranslationMemoryStatistics(
            total_entries=int(counts["total_entries"] or 0),
            exact_matches=int(metrics.get("exact_matches", 0)),
            provider_calls_saved=int(metrics.get("provider_calls_saved", 0)),
            estimated_api_cost_saved=float(metrics.get("estimated_api_cost_saved", 0.0)),
            estimated_time_saved_seconds=float(
                metrics.get("estimated_time_saved_seconds", 0.0)
            ),
            verified_entries=int(counts["verified_entries"] or 0),
            user_edited_entries=int(counts["user_edited_entries"] or 0),
            imported_entries=int(counts["imported_entries"] or 0),
        )

    def recent_entries(self, limit: int = 10) -> list[TranslationMemoryEntry]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tm_entries ORDER BY created_at DESC, id DESC LIMIT ?",
                (max(1, int(limit)),),
            ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def most_used_entries(self, limit: int = 10) -> list[TranslationMemoryEntry]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tm_entries
                ORDER BY usage_count DESC, last_used_at DESC, id DESC
                LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        return [_entry_from_row(row) for row in rows]

    def clear(self) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM tm_entries")
            connection.execute("DELETE FROM tm_metrics")

    def explain_lookup_plan(self) -> list[str]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT * FROM tm_entries
                WHERE source_text_hash = ?
                  AND source_language = ?
                  AND target_language = ?
                  AND region_type = ?
                  AND normalized_text = ?
                """,
                ("tmtext:v1:test", "ja", "en", "dialogue", "test"),
            ).fetchall()
        return [str(row["detail"]) for row in rows]

    def backup_to(self, destination: Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.resolve() == self.path.resolve():
            raise ValueError("Choose a different destination for the SQLite export.")
        source = self._connect()
        target = sqlite3.connect(destination)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        return destination


class TranslationMemory:
    """High-level exact-match TM with legacy JSON-cache compatibility."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        legacy_path: Path | None = None,
    ) -> None:
        self._legacy_lock = threading.RLock()
        self.configure(path, legacy_path=legacy_path)

    def configure(
        self,
        path: Path | None = None,
        *,
        legacy_path: Path | None = None,
    ) -> None:
        requested = Path(path) if path is not None else None
        if requested is not None and requested.suffix.casefold() == ".json":
            legacy_path = requested
            requested = requested.with_suffix(".db")
        with self._legacy_lock:
            self.database = TranslationMemoryDatabase(requested)
            self.path = self.database.path
            self.legacy_path = Path(
                legacy_path or PATHS.legacy_translation_memory
            )

    @staticmethod
    def legacy_key(
        *,
        engine_id: str,
        source_language: str,
        target_language: str,
        source_text: str,
        glossary: dict[str, str] | None = None,
    ) -> str:
        from hashlib import sha256

        payload: dict[str, Any] = {
            "kind": "global-translation-memory-v1",
            "engine": engine_id,
            "source_language": source_language,
            "target_language": target_language,
            "source_text": source_text.strip(),
            "glossary": glossary or {},
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()

    key = legacy_key

    def _legacy_get(
        self,
        *,
        engine_id: str,
        source_language: str,
        target_language: str,
        source_text: str,
        glossary: dict[str, str] | None,
    ) -> str | None:
        if not engine_id or not self.legacy_path.is_file():
            return None
        with self._legacy_lock:
            try:
                payload = json.loads(self.legacy_path.read_text(encoding="utf-8"))
            except (OSError, TypeError, ValueError):
                return None
        value = payload.get(self.legacy_key(
            engine_id=engine_id,
            source_language=source_language,
            target_language=target_language,
            source_text=source_text,
            glossary=glossary,
        ))
        return str(value) if isinstance(value, str) and value.strip() else None

    def lookup(
        self,
        *,
        source_text: str,
        source_language: str,
        target_language: str,
        region_type: str = "dialogue",
        prefer_verified: bool = True,
        engine_id: str = "",
        glossary: dict[str, str] | None = None,
        include_legacy: bool = True,
        record_usage: bool = True,
    ) -> TranslationMemoryMatch | None:
        entry = self.database.lookup(
            source_text=source_text,
            source_language=source_language,
            target_language=target_language,
            region_type=region_type,
            prefer_verified=prefer_verified,
            record_usage=record_usage,
        )
        if entry is not None:
            return TranslationMemoryMatch(entry=entry)
        if not include_legacy:
            return None
        legacy = self._legacy_get(
            engine_id=engine_id,
            source_language=source_language,
            target_language=target_language,
            source_text=source_text,
            glossary=glossary,
        )
        if legacy is None:
            return None
        synthesized = TranslationMemoryEntry(
            id=None,
            source_text=source_text,
            normalized_text=normalize_tm_source_text(source_text),
            source_text_hash=source_text_hash(source_text),
            source_region_hash=None,
            translated_text=legacy,
            source_language=_language_key(source_language),
            target_language=_language_key(target_language),
            region_type=normalize_region_type(region_type),
            translation_provider=engine_id,
            origin="provider",
        )
        return TranslationMemoryMatch(
            entry=synthesized,
            match_type="legacy-exact",
            source="legacy-cache",
        )

    def record(self, **values: Any) -> TranslationMemoryEntry | None:
        return self.database.record(**values)

    def record_user_edit(self, **values: Any) -> TranslationMemoryEntry | None:
        values.update(verified=True, user_edited=True, origin="user")
        return self.database.record(**values)

    def get(
        self,
        *,
        engine_id: str,
        source_language: str,
        target_language: str,
        source_text: str,
        glossary: dict[str, str] | None = None,
        region_type: str = "dialogue",
    ) -> str | None:
        match = self.lookup(
            engine_id=engine_id,
            source_language=source_language,
            target_language=target_language,
            source_text=source_text,
            glossary=glossary,
            region_type=region_type,
        )
        return match.translated_text if match else None

    def put(
        self,
        *,
        engine_id: str,
        source_language: str,
        target_language: str,
        source_text: str,
        translated_text: str,
        glossary: dict[str, str] | None = None,
        region_type: str = "dialogue",
    ) -> None:
        self.record(
            source_text=source_text,
            translated_text=translated_text,
            source_language=source_language,
            target_language=target_language,
            region_type=region_type,
            translation_provider=engine_id,
            origin="provider",
        )

    def statistics(self) -> TranslationMemoryStatistics:
        return self.database.statistics()

    def clear(self) -> None:
        self.database.clear()

    def record_provider_call_saved(
        self,
        *,
        estimated_api_cost: float = 0.0,
        estimated_time_seconds: float = 0.0,
    ) -> None:
        self.database.record_provider_call_saved(
            estimated_api_cost=estimated_api_cost,
            estimated_time_seconds=estimated_time_seconds,
        )

    def record_entry_hits(self, entry_ids: Iterable[int]) -> None:
        self.database.record_entry_hits(entry_ids)

    def export(self, destination: Path, format_name: str | None = None) -> Path:
        destination = Path(destination)
        format_key = (format_name or destination.suffix.lstrip(".")).casefold()
        if format_key in {"db", "sqlite", "sqlite3"}:
            return self.database.backup_to(destination)
        entries = self.database.all_entries()
        destination.parent.mkdir(parents=True, exist_ok=True)
        if format_key == "json":
            payload = {
                "format": "hydra-translation-memory",
                "version": SCHEMA_VERSION,
                "entries": [entry.to_dict() for entry in entries],
            }
            destination.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            return destination
        if format_key == "tmx":
            root = ET.Element("tmx", {"version": "1.4"})
            ET.SubElement(root, "header", {
                "creationtool": "Hydra Manga TL",
                "creationtoolversion": "1.0",
                "segtype": "sentence",
                "adminlang": "en",
                "srclang": "*all*",
                "datatype": "plaintext",
            })
            body = ET.SubElement(root, "body")
            xml_lang = "{http://www.w3.org/XML/1998/namespace}lang"
            for entry in entries:
                unit = ET.SubElement(body, "tu", {"tuid": str(entry.id or "")})
                for key in (
                    "normalized_text", "source_text_hash", "source_region_hash",
                    "region_type", "translation_provider", "provider_model",
                    "verified", "user_edited", "quality_score", "origin",
                    "series_id", "glossary_version", "project_id", "notes",
                ):
                    value = getattr(entry, key)
                    if value is not None and value != "":
                        prop = ET.SubElement(
                            unit,
                            "prop",
                            {"type": f"x-hydra-{key.replace('_', '-')}"},
                        )
                        prop.text = str(value)
                source_variant = ET.SubElement(
                    unit,
                    "tuv",
                    {xml_lang: entry.source_language},
                )
                ET.SubElement(source_variant, "seg").text = entry.source_text
                target_variant = ET.SubElement(
                    unit,
                    "tuv",
                    {xml_lang: entry.target_language},
                )
                ET.SubElement(target_variant, "seg").text = entry.translated_text
            ET.ElementTree(root).write(
                destination,
                encoding="utf-8",
                xml_declaration=True,
            )
            return destination
        raise ValueError(f"Unsupported Translation Memory export format: {format_key}")

    def import_file(self, source: Path, format_name: str | None = None) -> int:
        source = Path(source)
        format_key = (format_name or source.suffix.lstrip(".")).casefold()
        if format_key == "json":
            payload = json.loads(source.read_text(encoding="utf-8"))
            raw_entries = payload.get("entries", []) if isinstance(payload, dict) else []
        elif format_key == "tmx":
            raw_entries = list(self._read_tmx_entries(source))
        elif format_key in {"db", "sqlite", "sqlite3"}:
            if source.resolve() == self.path.resolve():
                raise ValueError(
                    "The selected SQLite file is already the active "
                    "Translation Memory."
                )
            raw_entries = list(self._read_sqlite_entries(source))
        else:
            raise ValueError(f"Unsupported Translation Memory import format: {format_key}")
        imported = 0
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            entry = self.record(
                source_text=str(raw.get("source_text", "")),
                translated_text=str(raw.get("translated_text", "")),
                source_language=str(raw.get("source_language", "")),
                target_language=str(raw.get("target_language", "")),
                region_type=str(raw.get("region_type", "dialogue")),
                source_region_hash=raw.get("source_region_hash") or None,
                translation_provider=str(raw.get("translation_provider", "")),
                provider_model=str(raw.get("provider_model", "")),
                verified=bool(raw.get("verified", True)),
                user_edited=bool(raw.get("user_edited", False)),
                quality_score=float(raw.get("quality_score", 0.0) or 0.0),
                origin="user" if raw.get("user_edited") else "imported",
                series_id=raw.get("series_id") or None,
                glossary_version=raw.get("glossary_version") or None,
                project_id=raw.get("project_id") or None,
                notes=raw.get("notes") or None,
            )
            imported += int(entry is not None)
        return imported

    @staticmethod
    def _read_tmx_entries(source: Path) -> Iterable[dict[str, Any]]:
        root = ET.parse(source).getroot()
        xml_lang = "{http://www.w3.org/XML/1998/namespace}lang"
        header = root.find("header")
        declared_source = str(
            header.get("srclang", "") if header is not None else ""
        ).casefold()
        for unit in root.findall(".//tu"):
            props = {
                str(prop.get("type", "")).removeprefix("x-hydra-").replace("-", "_"):
                    str(prop.text or "")
                for prop in unit.findall("prop")
            }
            variants = unit.findall("tuv")
            if len(variants) < 2:
                continue
            source_variant = next(
                (
                    variant for variant in variants
                    if declared_source not in {"", "*all*"}
                    and str(variant.get(xml_lang, "")).casefold()
                    == declared_source
                ),
                variants[0],
            )
            target_variant = next(
                variant for variant in variants
                if variant is not source_variant
            )
            source_segment = source_variant.find("seg")
            target_segment = target_variant.find("seg")
            if source_segment is None or target_segment is None:
                continue
            yield {
                **props,
                "source_text": "".join(source_segment.itertext()),
                "translated_text": "".join(target_segment.itertext()),
                "source_language": source_variant.get(xml_lang, ""),
                "target_language": target_variant.get(xml_lang, ""),
                "verified": props.get("verified", "true").casefold() in {"1", "true", "yes"},
                "user_edited": props.get("user_edited", "false").casefold() in {"1", "true", "yes"},
            }

    @staticmethod
    def _read_sqlite_entries(source: Path) -> Iterable[dict[str, Any]]:
        with closing(sqlite3.connect(source)) as connection:
            connection.row_factory = sqlite3.Row
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(tm_entries)")
            }
            required = {
                "source_text", "translated_text",
                "source_language", "target_language",
            }
            if not required.issubset(columns):
                raise ValueError("The selected SQLite file is not a Hydra Translation Memory.")
            for row in connection.execute("SELECT * FROM tm_entries"):
                yield dict(row)


class TranslationMemoryMatcher:
    def __init__(self, memory: TranslationMemory) -> None:
        self.memory = memory

    def match(self, **values: Any) -> TranslationMemoryMatch | None:
        return self.memory.lookup(**values)


TRANSLATION_MEMORY = TranslationMemory()
