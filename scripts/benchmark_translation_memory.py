"""Populate and benchmark indexed Translation Memory lookups.

Usage:
    python scripts/benchmark_translation_memory.py --sizes 100000 500000 1000000
"""

from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from hydra_manga_tl.translation.memory import TranslationMemory, source_text_hash


def populate(memory: TranslationMemory, count: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with closing(sqlite3.connect(memory.path)) as connection:
        for start in range(0, count, 10_000):
            rows = [
                (
                    f"source {index}",
                    f"source {index}",
                    source_text_hash(f"source {index}"),
                    f"translation {index}",
                    "japanese",
                    "en",
                    "dialogue",
                    now,
                    now,
                )
                for index in range(start, min(count, start + 10_000))
            ]
            connection.executemany(
                """
                INSERT OR IGNORE INTO tm_entries (
                    source_text, normalized_text, source_text_hash,
                    translated_text, source_language, target_language,
                    region_type, created_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        connection.commit()


def benchmark(size: int, samples: int) -> tuple[float, float, list[str]]:
    with tempfile.TemporaryDirectory() as folder:
        memory = TranslationMemory(
            Path(folder) / "translation_memory.db",
            legacy_path=Path(folder) / "legacy.json",
        )
        populate(memory, size)
        timings = []
        query = """
            SELECT translated_text FROM tm_entries
            WHERE source_text_hash = ?
              AND source_language = ?
              AND target_language = ?
              AND region_type = ?
              AND normalized_text = ?
            LIMIT 1
        """
        with closing(sqlite3.connect(memory.path)) as connection:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA cache_size = -32768")
            for index in range(samples):
                source = f"source {(index * 7919) % size}"
                identity = source_text_hash(source)
                started = time.perf_counter_ns()
                match = connection.execute(
                    query,
                    (identity, "japanese", "en", "dialogue", source),
                ).fetchone()
                timings.append(
                    (time.perf_counter_ns() - started) / 1_000_000
                )
                if match is None:
                    raise RuntimeError(f"Benchmark lookup missed {source}")
        ordered = sorted(timings)
        p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]
        return statistics.median(timings), p95, memory.database.explain_lookup_plan()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--sizes",
        nargs="+",
        type=int,
        default=[100_000, 500_000, 1_000_000],
    )
    parser.add_argument("--samples", type=int, default=500)
    args = parser.parse_args()
    for size in args.sizes:
        median, p95, plan = benchmark(size, args.samples)
        print(
            f"{size:,} entries: warm-sql median={median:.3f} ms "
            f"p95={p95:.3f} ms index={' | '.join(plan)}"
        )


if __name__ == "__main__":
    main()
