"""Chapter-level translation context and consistency memory."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ContextEngine:
    glossary: dict[str, str] = field(default_factory=dict)
    prior_page_memory: list[dict[str, Any]] = field(default_factory=list)
    name_memory: dict[str, str] = field(default_factory=dict)

    def page_context(self, dialogue: list[dict], layout_graph: dict[str, Any], page_number: int) -> str:
        surrounding = [
            f"{item.get('id')}: {item.get('text')}"
            for item in dialogue[:8]
            if str(item.get("text", "")).strip()
        ]
        prior = [
            f"{item.get('id')}: {item.get('translated_text')}"
            for item in self.prior_page_memory[-8:]
            if str(item.get("translated_text", "")).strip()
        ]
        glossary = ", ".join(f"{key}={value}" for key, value in sorted(self.glossary.items()))
        return (
            f"Chapter page {page_number}. Reading order: {', '.join(layout_graph.get('reading_order', []))}. "
            f"Surrounding bubbles: {' | '.join(surrounding)}. "
            f"Prior page memory: {' | '.join(prior)}. "
            f"Glossary and user overrides: {glossary or 'none'}. "
            "Keep character names, places, skills, honorific intent, and speaker references consistent. "
            "Never learn from unapproved OCR; use supplied OCR only as untrusted source evidence."
        )

    def remember_page(self, translated_groups: list[dict]) -> None:
        for group in translated_groups:
            original = str(group.get("original_text", "")).strip()
            translated = str(group.get("translated_text", "")).strip()
            if original and translated and len(original) <= 4 and not group.get("review_reasons"):
                self.name_memory.setdefault(original, translated)
            self.prior_page_memory.append({
                "id": f"r{group.get('index')}",
                "original_text": original,
                "translated_text": translated,
            })
        self.prior_page_memory = self.prior_page_memory[-40:]

