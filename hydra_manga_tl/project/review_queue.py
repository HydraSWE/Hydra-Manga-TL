"""AI subject identity and review-queue queries for workspace projects."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import uuid4


class ReviewQueueMixin:
    """Provide AI subject tracking and review queue queries to WorkspaceManager."""

    @staticmethod
    def _group_fingerprint(group: dict) -> str:
        geometry = group.get("source_polygons") or [group.get("polygon", [])]
        payload = json.dumps(
            {"geometry": geometry, "manual_id": group.get("manual_id", "")},
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def ai_subject_id(self, image_index: int, group: dict) -> str:
        if self.current is None:
            return ""
        image = self.current.images[image_index]
        fingerprint = self._group_fingerprint(group)
        subject = image.ai_subject_ids.get(fingerprint)
        if not subject:
            subject = str(uuid4())
            image.ai_subject_ids[fingerprint] = subject
            self.save()
        return subject

    def _existing_ai_subject_id(self, image_index: int, group: dict) -> str:
        if self.current is None:
            return ""
        image = self.current.images[image_index]
        return image.ai_subject_ids.get(self._group_fingerprint(group), "")

    def _is_ai_subject_approved(self, image_index: int, group: dict) -> bool:
        if self.current is None:
            return False
        subject = self._existing_ai_subject_id(image_index, group)
        return bool(
            subject
            and subject in self.current.images[image_index].approved_ai_subject_ids
        )

    def _mark_ai_subjects_approved(
        self, image_index: int, subject_ids: list[str]
    ) -> None:
        if self.current is None:
            return
        image = self.current.images[image_index]
        known = set(image.approved_ai_subject_ids)
        for subject in subject_ids:
            if subject and subject not in known:
                image.approved_ai_subject_ids.append(subject)
                known.add(subject)
        self._update_image_review_status(image_index)
        self.save()
        self.image_updated.emit(image_index)

    @staticmethod
    def ocr_review_reasons(group: dict) -> list[str]:
        text = str(group.get("original_text", "")).strip()
        confidence = float(group.get("ocr_confidence", 0.0) or 0.0)
        reasons: list[str] = []
        if confidence < 0.75:
            reasons.append("low_ocr_confidence")
        if any(char.isascii() and char.isdigit() for char in text):
            reasons.append("digit_like_ocr")
        if not text:
            reasons.append("empty_ocr")
        elif len(text) <= 2 and group.get("bubble_type") not in {"sfx", "sign"}:
            reasons.append("very_short_text")
        if text and not any(
            "\u3040" <= char <= "\u30ff" or "\u3400" <= char <= "\u9fff"
            for char in text
        ):
            reasons.append("no_japanese_script")
        for reason in group.get("review_reasons", []):
            reason = str(reason)
            if reason.startswith(("ocr", "low_confidence", "script", "digit")):
                reasons.append(reason)
        return list(dict.fromkeys(reasons))

    def ocr_review_queue(self) -> list[dict]:
        if self.current is None:
            return []
        queue: list[dict] = []
        for image_index, image in enumerate(self.current.images):
            if not image.translation_result or not Path(image.translation_result).is_file():
                continue
            try:
                groups = self.effective_translation_payload(image_index).get(
                    "translation_groups", []
                )
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            for block_index, group in enumerate(groups):
                if self._is_ai_subject_approved(image_index, group):
                    continue
                reasons = self.ocr_review_reasons(group)
                if not reasons:
                    continue
                queue.append(
                    {
                        "image_index": image_index,
                        "block_index": block_index,
                        "group_index": group.get("index"),
                        "image_id": image.id,
                        "page": image_index + 1,
                        "text": str(group.get("original_text", "")),
                        "confidence": float(group.get("ocr_confidence", 0.0) or 0.0),
                        "reasons": reasons,
                    }
                )
        return sorted(
            queue,
            key=lambda item: (item["confidence"], item["page"], str(item["group_index"])),
        )

    def review_issue_queue(self) -> list[dict]:
        if self.current is None:
            return []
        queue: list[dict] = []
        for image_index, image in enumerate(self.current.images):
            if not image.translation_result or not Path(image.translation_result).is_file():
                continue
            try:
                groups = self.effective_translation_payload(image_index).get(
                    "translation_groups", []
                )
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            for block_index, group in enumerate(groups):
                if self._is_ai_subject_approved(image_index, group):
                    continue
                ocr_reasons = set(self.ocr_review_reasons(group))
                reasons = [
                    str(reason)
                    for reason in group.get("review_reasons", [])
                    if str(reason) and str(reason) not in ocr_reasons
                ]
                if group.get("status") == "review" and not reasons and not ocr_reasons:
                    reasons = ["review_required"]
                if not reasons:
                    continue
                queue.append(
                    {
                        "image_index": image_index,
                        "block_index": block_index,
                        "group_index": group.get("index"),
                        "image_id": image.id,
                        "page": image_index + 1,
                        "text": str(
                            group.get("translated_text")
                            or group.get("original_text")
                            or ""
                        ),
                        "confidence": float(group.get("ocr_confidence", 0.0) or 0.0),
                        "reasons": list(dict.fromkeys(reasons)),
                    }
                )
        return sorted(
            queue,
            key=lambda item: (item["page"], item["block_index"], str(item["group_index"])),
        )
