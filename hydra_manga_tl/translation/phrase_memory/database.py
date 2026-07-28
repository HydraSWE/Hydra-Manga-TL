"""SQLite-backed Phrase Memory (PM v1) database implementation."""

from __future__ import annotations

from contextlib import closing, contextmanager

from dataclasses import fields
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any, Iterable

from hydra_manga_tl.core.paths import PATHS
from hydra_manga_tl.translation.memory.fingerprints import normalize_tm_source_text
from .models import (
    PhraseMemoryEntry,
    PhraseMemoryMatch,
    PhraseMemoryStatistics,
)

SCHEMA_VERSION = 1
PHRASE_HASH_PREFIX = "pmphrase:v1:"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _language_key(value: str) -> str:
    return str(value or "").strip().casefold()


def source_phrase_hash(phrase: str) -> str:
    normalized = normalize_tm_source_text(phrase)
    return PHRASE_HASH_PREFIX + sha256(normalized.encode("utf-8")).hexdigest()


def _entry_from_row(row: sqlite3.Row) -> PhraseMemoryEntry:
    values = dict(row)
    values["verified"] = bool(values.get("verified"))
    return PhraseMemoryEntry(**{
        field.name: values.get(field.name)
        for field in fields(PhraseMemoryEntry)
    })


class PhraseMemoryDatabase:
    """SQLite-backed database for Phrase Memory entries and metrics."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or PATHS.phrase_memory)
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
                    CREATE TABLE IF NOT EXISTS pm_entries (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        source_phrase TEXT NOT NULL,
                        normalized_phrase TEXT NOT NULL,
                        source_phrase_hash TEXT NOT NULL,
                        target_phrase TEXT NOT NULL,
                        source_language TEXT NOT NULL,
                        target_language TEXT NOT NULL,
                        verified INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        usage_count INTEGER NOT NULL DEFAULT 0,
                        confidence REAL NOT NULL DEFAULT 1.0,
                        origin TEXT NOT NULL DEFAULT 'QA',
                        project_id TEXT,
                        series_id TEXT,
                        notes TEXT
                    );
                    CREATE UNIQUE INDEX IF NOT EXISTS ux_pm_entry_candidate
                    ON pm_entries (
                        source_phrase_hash, normalized_phrase, source_language,
                        target_language, target_phrase
                    );
                    CREATE INDEX IF NOT EXISTS ix_pm_phrase_lookup
                    ON pm_entries (
                        source_phrase_hash, source_language, target_language, normalized_phrase
                    );
                    CREATE TABLE IF NOT EXISTS pm_metrics (
                        key TEXT PRIMARY KEY,
                        value REAL NOT NULL DEFAULT 0.0
                    );
                    """
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    def lookup_phrase(
        self,
        *,
        source_phrase: str,
        source_language: str,
        target_language: str,
        prefer_verified: bool = True,
        record_usage: bool = True,
    ) -> PhraseMemoryMatch | None:
        matches = self.lookup_candidates(
            source_phrases=[source_phrase],
            source_language=source_language,
            target_language=target_language,
            prefer_verified=prefer_verified,
            record_usage=record_usage,
        )
        return matches[0] if matches else None

    def lookup_candidates(
        self,
        *,
        source_phrases: Iterable[str],
        source_language: str,
        target_language: str,
        prefer_verified: bool = True,
        record_usage: bool = False,
    ) -> list[PhraseMemoryMatch]:
        results: list[PhraseMemoryMatch] = []
        with self._connection() as connection:
            for phrase in source_phrases:
                normalized = normalize_tm_source_text(phrase)
                if not normalized:
                    continue
                identity = source_phrase_hash(normalized)
                priority = (
                    """
                    CASE
                        WHEN verified = 1 THEN 3
                        WHEN origin IN ('User Edit', 'user', 'Glossary', 'glossary') THEN 2
                        ELSE 1
                    END DESC,
                    """
                    if prefer_verified
                    else ""
                )
                query = f"""
                    SELECT * FROM pm_entries
                    WHERE source_phrase_hash = ?
                      AND source_language = ?
                      AND target_language = ?
                      AND normalized_phrase = ?
                    ORDER BY
                      {priority}
                      confidence DESC,
                      usage_count DESC,
                      updated_at DESC,
                      id DESC
                    LIMIT 1
                """
                row = connection.execute(
                    query,
                    (
                        identity,
                        _language_key(source_language),
                        _language_key(target_language),
                        normalized,
                    ),
                ).fetchone()
                if row is not None:
                    entry = _entry_from_row(row)
                    if record_usage and entry.id is not None:
                        now = _utc_now()
                        connection.execute(
                            """
                            UPDATE pm_entries
                            SET usage_count = usage_count + 1, updated_at = ?
                            WHERE id = ?
                            """,
                            (now, entry.id),
                        )
                        self._increment_metric(connection, "total_matches", 1.0)
                    results.append(PhraseMemoryMatch(entry=entry))
        return results

    def all_entries_for_languages(
        self,
        source_language: str,
        target_language: str,
    ) -> list[PhraseMemoryEntry]:
        query = """
            SELECT * FROM pm_entries
            WHERE source_language = ? AND target_language = ?
            ORDER BY verified DESC, usage_count DESC, updated_at DESC
        """
        with self._connection() as connection:
            rows = connection.execute(
                query,
                (_language_key(source_language), _language_key(target_language)),
            ).fetchall()
            return [_entry_from_row(row) for row in rows]

    def record(
        self,
        *,
        source_phrase: str,
        target_phrase: str,
        source_language: str,
        target_language: str,
        verified: bool = False,
        confidence: float = 1.0,
        origin: str = "QA",
        project_id: str | None = None,
        series_id: str | None = None,
        notes: str | None = None,
    ) -> PhraseMemoryEntry | None:
        normalized_src = normalize_tm_source_text(source_phrase)
        normalized_tgt = target_phrase.strip()
        if not normalized_src or not normalized_tgt:
            return None
        identity = source_phrase_hash(normalized_src)
        now = _utc_now()
        src_lang = _language_key(source_language)
        tgt_lang = _language_key(target_language)

        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO pm_entries (
                    source_phrase, normalized_phrase, source_phrase_hash,
                    target_phrase, source_language, target_language,
                    verified, created_at, updated_at, usage_count,
                    confidence, origin, project_id, series_id, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT (source_phrase_hash, normalized_phrase, source_language, target_language, target_phrase)
                DO UPDATE SET
                    updated_at = excluded.updated_at,
                    verified = CASE WHEN excluded.verified = 1 THEN 1 ELSE pm_entries.verified END,
                    confidence = MAX(pm_entries.confidence, excluded.confidence),
                    origin = COALESCE(excluded.origin, pm_entries.origin),
                    project_id = COALESCE(excluded.project_id, pm_entries.project_id),
                    series_id = COALESCE(excluded.series_id, pm_entries.series_id),
                    notes = COALESCE(excluded.notes, pm_entries.notes)
                """,
                (
                    source_phrase.strip(),
                    normalized_src,
                    identity,
                    normalized_tgt,
                    src_lang,
                    tgt_lang,
                    int(verified),
                    now,
                    now,
                    confidence,
                    origin,
                    project_id,
                    series_id,
                    notes,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM pm_entries
                WHERE source_phrase_hash = ?
                  AND source_language = ?
                  AND target_language = ?
                  AND target_phrase = ?
                """,
                (identity, src_lang, tgt_lang, normalized_tgt),
            ).fetchone()
            self._increment_metric(connection, "learned_count", 1.0)
            return _entry_from_row(row) if row is not None else None

    def record_user_edit(
        self,
        *,
        source_phrase: str,
        target_phrase: str,
        source_language: str,
        target_language: str,
        project_id: str | None = None,
        series_id: str | None = None,
    ) -> PhraseMemoryEntry | None:
        return self.record(
            source_phrase=source_phrase,
            target_phrase=target_phrase,
            source_language=source_language,
            target_language=target_language,
            verified=True,
            confidence=1.0,
            origin="User Edit",
            project_id=project_id,
            series_id=series_id,
        )

    def record_hits(self, entry_ids: Iterable[int]) -> None:
        now = _utc_now()
        ids = [int(i) for i in entry_ids]
        if not ids:
            return
        with self._connection() as connection:
            placeholders = ",".join("?" * len(ids))
            connection.execute(
                f"""
                UPDATE pm_entries
                SET usage_count = usage_count + 1, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                (now, *ids),
            )
            self._increment_metric(connection, "total_matches", float(len(ids)))

    def statistics(self) -> PhraseMemoryStatistics:
        with self._connection() as connection:
            total_entries = int(
                connection.execute("SELECT COUNT(*) FROM pm_entries").fetchone()[0]
            )
            verified_entries = int(
                connection.execute(
                    "SELECT COUNT(*) FROM pm_entries WHERE verified = 1"
                ).fetchone()[0]
            )
            total_matches = int(self._get_metric(connection, "total_matches"))
            learned_count = int(self._get_metric(connection, "learned_count"))
            saved_api_cost = self._get_metric(connection, "saved_api_cost")
            saved_time_seconds = self._get_metric(connection, "saved_time_seconds")
            return PhraseMemoryStatistics(
                total_entries=total_entries,
                verified_entries=verified_entries,
                total_matches=total_matches,
                learned_count=learned_count,
                saved_api_cost=saved_api_cost,
                saved_time_seconds=saved_time_seconds,
            )

    def clear(self) -> None:
        with self._connection() as connection:
            connection.execute("DELETE FROM pm_entries")
            connection.execute("DELETE FROM pm_metrics")

    def delete_entry(self, entry_id: int) -> bool:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM pm_entries WHERE id = ?", (entry_id,))
            return cursor.rowcount > 0

    def update_entry(
        self,
        entry_id: int,
        *,
        source_phrase: str | None = None,
        target_phrase: str | None = None,
        verified: bool | None = None,
        notes: str | None = None,
    ) -> bool:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM pm_entries WHERE id = ?", (entry_id,)).fetchone()
            if row is None:
                return False
            src = source_phrase if source_phrase is not None else row["source_phrase"]
            tgt = target_phrase if target_phrase is not None else row["target_phrase"]
            ver = int(verified) if verified is not None else row["verified"]
            nts = notes if notes is not None else row["notes"]
            norm_src = normalize_tm_source_text(src)
            identity = source_phrase_hash(norm_src)
            now = _utc_now()
            connection.execute(
                """
                UPDATE pm_entries
                SET source_phrase = ?, normalized_phrase = ?, source_phrase_hash = ?,
                    target_phrase = ?, verified = ?, updated_at = ?, notes = ?
                WHERE id = ?
                """,
                (src.strip(), norm_src, identity, tgt.strip(), ver, now, nts, entry_id),
            )
            return True

    def toggle_verified(self, entry_id: int) -> bool:
        with self._connection() as connection:
            now = _utc_now()
            cursor = connection.execute(
                """
                UPDATE pm_entries
                SET verified = CASE WHEN verified = 1 THEN 0 ELSE 1 END, updated_at = ?
                WHERE id = ?
                """,
                (now, entry_id),
            )
            return cursor.rowcount > 0

    def get_entry(self, entry_id: int) -> PhraseMemoryEntry | None:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM pm_entries WHERE id = ?", (entry_id,)).fetchone()
            return _entry_from_row(row) if row is not None else None


    def backup_to(self, destination: Path) -> Path:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as src_conn, closing(
            sqlite3.connect(destination)
        ) as dst_conn:
            src_conn.backup(dst_conn)
        return destination

    def export(self, destination: Path) -> Path:
        destination = Path(destination)
        ext = destination.suffix.lstrip(".").casefold()
        if ext in {"pmdb", "db", "sqlite", "sqlite3"}:
            return self.backup_to(destination)
        entries = self.all_entries()
        payload = {
            "format": "hydra-phrase-memory",
            "version": SCHEMA_VERSION,
            "entries": [entry.to_dict() for entry in entries],
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return destination

    def import_file(self, source: Path) -> int:
        source = Path(source)
        ext = source.suffix.lstrip(".").casefold()
        if ext in {"pmdb", "db", "sqlite", "sqlite3"}:
            if source.resolve() == self.path.resolve():
                raise ValueError("Cannot import active database file into itself.")
            with closing(sqlite3.connect(source)) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute("SELECT * FROM pm_entries").fetchall()
                raw_entries = [dict(row) for row in rows]
        else:
            payload = json.loads(source.read_text(encoding="utf-8"))
            raw_entries = payload.get("entries", []) if isinstance(payload, dict) else []

        imported = 0
        for raw in raw_entries:
            if not isinstance(raw, dict):
                continue
            entry = self.record(
                source_phrase=str(raw.get("source_phrase", "")),
                target_phrase=str(raw.get("target_phrase", "")),
                source_language=str(raw.get("source_language", "")),
                target_language=str(raw.get("target_language", "")),
                verified=bool(raw.get("verified", False)),
                confidence=float(raw.get("confidence", 1.0) or 1.0),
                origin=str(raw.get("origin", "Imported")),
                project_id=raw.get("project_id"),
                series_id=raw.get("series_id"),
                notes=raw.get("notes"),
            )
            imported += int(entry is not None)
        return imported

    def all_entries(self) -> list[PhraseMemoryEntry]:
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM pm_entries ORDER BY id").fetchall()
            return [_entry_from_row(row) for row in rows]

    def _get_metric(self, connection: sqlite3.Connection, key: str) -> float:
        row = connection.execute(
            "SELECT value FROM pm_metrics WHERE key = ?", (key,)
        ).fetchone()
        return float(row["value"]) if row else 0.0

    def _increment_metric(
        self, connection: sqlite3.Connection, key: str, amount: float
    ) -> None:
        connection.execute(
            """
            INSERT INTO pm_metrics (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = value + excluded.value
            """,
            (key, amount),
        )
