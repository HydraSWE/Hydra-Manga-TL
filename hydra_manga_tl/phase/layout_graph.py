"""Graph-based OCR layout analysis for manga reading order and dialogue units."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from hydra_manga_tl.phase.layout import TextGroup, group_regions


@dataclass(frozen=True)
class LayoutNode:
    id: str
    region_index: int
    text: str
    bbox: list[int]
    direction: str
    confidence: float


@dataclass(frozen=True)
class LayoutEdge:
    source: str
    target: str
    kind: str
    weight: float


@dataclass(frozen=True)
class LayoutGraphResult:
    nodes: list[LayoutNode]
    edges: list[LayoutEdge]
    reading_order: list[str]
    dialogue_groups: list[dict[str, Any]]
    translation_units: list[dict[str, Any]]
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nodes": [asdict(node) for node in self.nodes],
            "edges": [asdict(edge) for edge in self.edges],
            "reading_order": self.reading_order,
            "dialogue_groups": self.dialogue_groups,
            "translation_units": self.translation_units,
            "metadata": self.metadata,
        }


def _box(region: dict) -> list[int]:
    xs = [int(point[0]) for point in region["polygon"]]
    ys = [int(point[1]) for point in region["polygon"]]
    return [min(xs), min(ys), max(xs), max(ys)]


def _center(box: list[int]) -> tuple[float, float]:
    return (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0


def _overlap_ratio(a1: int, a2: int, b1: int, b2: int) -> float:
    overlap = max(0, min(a2, b2) - max(a1, b1))
    return overlap / max(1, min(a2 - a1, b2 - b1))


def _reading_key(node: LayoutNode) -> tuple[int, int, int]:
    x1, y1, x2, y2 = node.bbox
    width, height = max(1, x2 - x1), max(1, y2 - y1)
    column = round(((x1 + x2) / 2) / max(1, width * 1.4))
    if node.direction == "vertical-rtl" or height > width * 1.5:
        return (-column, y1, -x1)
    return (y1, -x1, x1)


def build_layout_graph(regions: list[dict], *, page_size: tuple[int, int] | None = None) -> LayoutGraphResult:
    nodes = [
        LayoutNode(
            id=f"r{index}",
            region_index=index,
            text=str(region.get("text", "")),
            bbox=_box(region),
            direction="vertical-rtl" if (_box(region)[3] - _box(region)[1]) > max(1, (_box(region)[2] - _box(region)[0])) * 1.5 else "horizontal-ltr",
            confidence=float(region.get("confidence", 0.0)),
        )
        for index, region in enumerate(regions, 1)
        if region.get("polygon")
    ]
    edges: list[LayoutEdge] = []
    for left in nodes:
        for right in nodes:
            if left.id >= right.id:
                continue
            lx, ly = _center(left.bbox)
            rx, ry = _center(right.bbox)
            horizontal_gap = abs(lx - rx)
            vertical_gap = abs(ly - ry)
            vertical_overlap = _overlap_ratio(left.bbox[1], left.bbox[3], right.bbox[1], right.bbox[3])
            horizontal_overlap = _overlap_ratio(left.bbox[0], left.bbox[2], right.bbox[0], right.bbox[2])
            if vertical_overlap >= 0.45:
                edges.append(LayoutEdge(left.id, right.id, "vertical_alignment", round(vertical_overlap, 3)))
            if horizontal_overlap >= 0.45:
                edges.append(LayoutEdge(left.id, right.id, "horizontal_proximity", round(horizontal_overlap, 3)))
            if horizontal_gap <= 120 and vertical_gap <= 180:
                edges.append(LayoutEdge(left.id, right.id, "bubble_proximity", round(1.0 / max(1.0, horizontal_gap + vertical_gap), 4)))
            if page_size and horizontal_gap <= page_size[0] * 0.35 and vertical_gap <= page_size[1] * 0.35:
                edges.append(LayoutEdge(left.id, right.id, "panel_proximity", round(1.0 / max(1.0, horizontal_gap + vertical_gap), 4)))

    ordered_nodes = sorted(nodes, key=_reading_key)
    groups: list[TextGroup] = group_regions(regions)
    dialogue_groups = [
        {
            "id": f"g{index}",
            "member_region_indices": group.member_indices,
            "text": group.text,
            "bbox": list(group.bbox),
            "direction": group.direction,
            "reading_order": index,
        }
        for index, group in enumerate(groups, 1)
    ]
    translation_units = [
        {
            "id": f"u{index}",
            "group_id": group["id"],
            "source_region_ids": [f"r{member}" for member in group["member_region_indices"]],
            "text": group["text"],
            "bbox": group["bbox"],
            "direction": group["direction"],
            "reading_order": group["reading_order"],
        }
        for index, group in enumerate(dialogue_groups, 1)
    ]
    return LayoutGraphResult(
        nodes=nodes,
        edges=edges,
        reading_order=[node.id for node in ordered_nodes],
        dialogue_groups=dialogue_groups,
        translation_units=translation_units,
        metadata={"page_size": list(page_size) if page_size else []},
    )

