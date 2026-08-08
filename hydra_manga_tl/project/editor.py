"""Persistent Phase 4 project model and non-destructive translation overrides."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field
import json
from pathlib import Path

from hydra_manga_tl.core.normalization import normalize_global_text
from hydra_manga_tl.core.region_types import normalize_region_type


@dataclass
class RegionEdit:
    translated_text: str | None = None
    replace: bool = True
    font_size: int = 0
    offset_x: int = 0
    offset_y: int = 0
    font_family: str = "Arial"
    color: str = "#111111"
    alignment: str = "center"
    original_text: str | None = None
    bubble_type: str | None = None
    layout_x: int | None = None
    layout_y: int | None = None
    layout_width: int | None = None
    layout_height: int | None = None
    layout_angle: float | None = None
    style_profile: dict | None = None


@dataclass
class EditorDocument:
    result_path: str
    edits: dict[str, RegionEdit] = field(default_factory=dict)


@dataclass
class EditorProject:
    version: int = 1
    documents: list[EditorDocument] = field(default_factory=list)
    selected_document: int = 0

    @classmethod
    def from_phase2(cls, folder: Path) -> "EditorProject":
        files = sorted(folder.glob("*_translated_*.json"))
        if not files:
            raise ValueError("No Phase 2 translated results were found.")
        return cls(documents=[EditorDocument(str(path.resolve())) for path in files])

    @classmethod
    def load(cls, path: Path) -> "EditorProject":
        payload = json.loads(path.read_text(encoding="utf-8"))
        documents = []
        for document in payload.get("documents", []):
            edits = {key: RegionEdit(**value) for key, value in document.get("edits", {}).items()}
            documents.append(EditorDocument(document["result_path"], edits))
        project = cls(payload.get("version", 1), documents, payload.get("selected_document", 0))
        if not project.documents:
            raise ValueError("The editor project contains no documents.")
        return project

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")

    def phase2_payload(self, document_index: int) -> dict:
        document = self.documents[document_index]
        return json.loads(Path(document.result_path).read_text(encoding="utf-8"))

    def effective_payload(self, document_index: int) -> dict:
        payload = deepcopy(self.phase2_payload(document_index))
        edits = self.documents[document_index].edits
        for group in payload["translation_groups"]:
            edit = edits.get(str(group["index"]))
            if edit is None:
                continue
            if edit.translated_text is not None:
                group["translated_text"] = normalize_global_text(edit.translated_text)
            group["editor_replace"] = edit.replace
            group["font_size_override"] = edit.font_size
            group["placement_offset"] = [edit.offset_x, edit.offset_y]
            if edit.bubble_type is not None:
                region_type = normalize_region_type(edit.bubble_type)
                group["bubble_type"] = region_type
                group["type"] = region_type
            if all(value is not None for value in (edit.layout_x, edit.layout_y, edit.layout_width, edit.layout_height)):
                group["text_layout"] = {
                    "x": edit.layout_x,
                    "y": edit.layout_y,
                    "width": edit.layout_width,
                    "height": edit.layout_height,
                    "angle": edit.layout_angle or 0.0,
                }
            if edit.style_profile is not None:
                group["style_profile"] = dict(edit.style_profile)
        return payload

    def update_edit(self, document_index: int, group_index: int, edit: RegionEdit) -> None:
        self.documents[document_index].edits[str(group_index)] = edit
