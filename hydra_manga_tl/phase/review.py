"""AI-style deterministic review checks for OCR, translation, and render output."""

from __future__ import annotations

from collections import Counter
from typing import Any


def review_translation_groups(groups: list[dict], render_review: dict[str, Any] | None = None) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    translated_counts = Counter(str(group.get("translated_text", "")).strip() for group in groups if group.get("translated_text"))
    render_by_group = {
        int(item.get("group", 0)): list(item.get("reasons", []))
        for item in (render_review or {}).get("issues", [])
    }
    seen_names: dict[str, str] = {}

    for group in groups:
        reasons = list(group.get("review_reasons", []))
        original = str(group.get("original_text", "")).strip()
        translated = str(group.get("translated_text", "")).strip()
        confidence = float(group.get("ocr_confidence", 0.0) or 0.0)
        if confidence < 0.70:
            reasons.append("suspicious_ocr")
        if original and translated and original == translated:
            reasons.append("unchanged_translation")
        if translated and translated_counts[translated] > 1:
            reasons.append("repeated_translation")
        if original and len(original) <= 4 and translated:
            locked = seen_names.setdefault(original, translated)
            if locked != translated:
                reasons.append("name_drift")
        reasons.extend(render_by_group.get(int(group.get("index", 0) or 0), []))
        reasons = list(dict.fromkeys(reasons))
        if reasons:
            issues.append({
                "group": int(group.get("index", 0) or 0),
                "reasons": reasons,
                "original_text": original,
                "translated_text": translated,
                "confidence": confidence,
            })
            group["review_reasons"] = reasons
            if group.get("status") == "translated":
                group["status"] = "review"
    return {"issue_count": len(issues), "issues": issues}

