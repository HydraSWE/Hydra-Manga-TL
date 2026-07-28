"""Professional text fitting and render review helpers."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class TypesetReview:
    group: int
    reasons: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def review_rendered_group(group: dict, render_details: dict, page_size: tuple[int, int]) -> TypesetReview:
    reasons: list[str] = []
    box = list(render_details.get("box") or group.get("safe_area") or group.get("polygon", [[0, 0], [0, 0], [0, 0], [0, 0]])[0])
    font_size = int(render_details.get("font_size", 0) or 0)
    lines = list(render_details.get("lines", []))
    if font_size and font_size < 9:
        reasons.append("too_small_font")
    if render_details.get("overflow"):
        reasons.append("text_overflow")
    if len(box) == 4:
        margin = min(box[0], box[1], page_size[0] - box[2], page_size[1] - box[3])
        if margin < 2:
            reasons.append("unsafe_margin")
    if len(lines) >= 2 and len(set(line.strip() for line in lines if line.strip())) < len([line for line in lines if line.strip()]):
        reasons.append("repeated_render_line")
    return TypesetReview(
        group=int(group.get("index", render_details.get("group", 0)) or 0),
        reasons=list(dict.fromkeys(reasons)),
        metrics={"font_size": font_size, "line_count": len(lines), "box": box},
    )


def summarize_render_review(reviews: list[TypesetReview]) -> dict[str, Any]:
    issues = [review.to_dict() for review in reviews if review.reasons]
    return {
        "issue_count": len(issues),
        "issues": issues,
        "fit_without_manual_resize_count": sum(1 for review in reviews if not review.reasons),
        "reviewed_count": len(reviews),
    }

