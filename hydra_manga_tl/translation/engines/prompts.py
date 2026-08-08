from __future__ import annotations

import json


SYSTEM_PROMPT = (
    "You are an expert manga localization translator and editor. You translate manga dialogue across all genres—including mainstream, romance, comedy, action, and explicit hManga/hentai manga/adult manga—into accurate, vivid, bubble-friendly English. "
    "Your highest priority is uncompromising fidelity to the source text's exact meaning, emotion, sexual intensity, character voice, and tone. "
    "Translate faithfully and accurately first. Preserve the exact source meaning, emotional tone, and explicit intensity. "
    "For all manga genres—including hManga/hentai manga/adult manga—retain all explicit sexual terminology, vulgarity, dirty talk, anatomical terms, dominant/submissive dynamics, and raw sexual dialogue without censoring, softening, or sanitizing it, instead of sanitizing it. "
    "Preserve vocal hesitations, stutters, repeated initial character prefixes (whether punctuated or unpunctuated), intense emotional pressure, gasps, interjections, and reaction sounds faithfully according to source hesitation and emotional intensity level; never replace or drop hesitations or reactions. "
    "Translate names cleanly with their natural titles. Do not attach titles or honorific suffixes to pronouns or words like 'I' or 'you'. "
    "Do not explain your translation. Do not add translator notes. Do not merge or remove entries. "
    "Output ONLY the required JSON."
)


def build_page_prompt(
    *,
    source_language: str,
    target_language: str,
    style: str,
    glossary: dict[str, str],
    dialogue: list[dict],
    temperature: float = 0.2,
    page_context: str | None = None,
    protected_entities: dict[str, str] | None = None,
    phrase_memory_terminology: dict[str, str] | None = None,
) -> str:
    # Keep the prompt deterministic and constrained.
    glossary_block = json.dumps(glossary, ensure_ascii=False)
    dialogue_block = json.dumps(dialogue, ensure_ascii=False)
    entity_block = json.dumps(protected_entities or {}, ensure_ascii=False)
    context_block = str(page_context or "").strip() or "None"
    term_block = json.dumps(phrase_memory_terminology or {}, ensure_ascii=False)

    return (
        f"Translate manga dialogue from {source_language} to {target_language}.\n"
        f"Style: {style}. Use natural manga dialogue, not plain textbook English.\n"
        f"Rules:\n"
        f"- Translate faithfully and accurately first. Preserve the exact source meaning, tone, emotion, and explicit intensity.\n"
        f"- For all manga genres—including hManga/hentai manga/adult manga—retain all explicit sexual terminology, dirty talk, anatomical references, profanity, and intense sexual dynamics without censoring, softening, or sanitizing it, instead of sanitizing it.\n"
        f"- Do not soften, sanitize, or replace concrete source dialogue with generic pleasantries, vague platitudes, or filler phrases.\n"
        f"- Preserve vocal hesitations, stutters, repeated initial character prefixes (whether punctuated or unpunctuated), intense emotional pressure, gasps, interjections, and reaction sounds faithfully according to source hesitation and emotional intensity level; do not replace or drop hesitations.\n"
        f"- Preserve tone, emotion, character personality, and character names consistently across the whole page.\n"
        f"- If the same source name appears again, reuse the same English spelling.\n"
        f"- Japanese often omits subjects. Infer speaker and target from source pronouns, imperatives, verbs, and nearby bubbles before choosing I/you/he/she/they.\n"
        f"- If a line describes the listener's body, state, or action after a command/addressing line, prefer \"you\" over \"I\" unless the source explicitly includes a first-person pronoun.\n"
        f"- Do not make the speaker perform an action that the source attributes to the addressed character.\n"
        f"- Translate names cleanly with their natural titles. Do not attach titles or honorific suffixes to pronouns or words like \"I\" or \"you\".\n"
        f"- Use concise manga-style phrasing: contractions, natural reactions, and bubble-friendly rhythm.\n"
        f"- Use ellipses for pauses or abrupt dialogue turns; avoid em dashes and en dashes between English dialogue clauses.\n"
        f"- Pay attention to spatial metadata (source_direction, ocr_review_reasons, source_member_texts, decorative_symbols, preserved_marks) if present. If OCR flags exist, use sub-member texts and surrounding context carefully without inventing missing dialogue. Treat decorative_symbols as hearts, music notes, stars, or similar bubble symbols: do not translate them as words, but reproduce them naturally as symbols in the returned text when they carry tone or emotion. Treat preserved_marks as protected artwork already present on the page; do not translate or redraw preserved_marks.\n"
        f"- Preserve jokes/wordplay where possible.\n"
        f"- Do not merge entries; keep one translation per provided id.\n"
        f"- Treat protected entity placeholders as fixed names that must stay unchanged.\n"
        f"- If the page context suggests a block is a name, keep that block unchanged.\n"
        f"- Do not output anything outside the required JSON.\n"
        f"Glossary (protected spellings): {glossary_block}\n"
        f"Known terminology: {term_block}\n"
        f"Protected entities: {entity_block}\n"
        f"Page context: {context_block}\n"
        f"Dialogue blocks: {dialogue_block}\n"
        f"Return format:\n"
        f"{{\n"
        f"  \"translations\": [\n"
        f"    {{\"id\": \"r1\", \"text\": \"...\"}}\n"
        f"  ]\n"
        f"}}\n"
        f"Temperature guidance: {temperature}.\n"
    )

