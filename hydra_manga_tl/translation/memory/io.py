"""Import/export helpers for Hydra Translation Memory files."""

from __future__ import annotations

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterable
import xml.etree.ElementTree as ET

from .models import TranslationMemoryEntry


def write_json_export(
    destination: Path,
    entries: list[TranslationMemoryEntry],
    *,
    schema_version: int,
) -> Path:
    payload = {
        "format": "hydra-translation-memory",
        "version": schema_version,
        "entries": [entry.to_dict() for entry in entries],
    }
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return destination


def write_tmx_export(destination: Path, entries: list[TranslationMemoryEntry]) -> Path:
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


def read_json_entries(source: Path) -> Iterable[dict[str, Any]]:
    payload = json.loads(source.read_text(encoding="utf-8"))
    raw_entries = payload.get("entries", []) if isinstance(payload, dict) else []
    return raw_entries


def read_tmx_entries(source: Path) -> Iterable[dict[str, Any]]:
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


def read_sqlite_entries(source: Path) -> Iterable[dict[str, Any]]:
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
