from __future__ import annotations

import json


SYSTEM_PROMPT = (
    "You are a professional manga localization editor. You translate manga dialogue into vivid, bubble-friendly English. "
    "Preserve meaning, tone, emotion, character names, relationships, jokes and wordplay where possible. "
    "Keep same-page names consistent, including honorifics such as senpai when relationship context matters. "
    "Infer omitted Japanese subjects from pronouns, commands, body/action descriptions, and neighboring bubbles. "
    "Do not censor ordinary dialogue. Do not explain your translation. "
    "Do not add notes. Do not merge or remove entries. "
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
) -> str:
    # Keep the prompt deterministic and constrained.
    glossary_block = json.dumps(glossary, ensure_ascii=False)
    dialogue_block = json.dumps(dialogue, ensure_ascii=False)
    entity_block = json.dumps(protected_entities or {}, ensure_ascii=False)
    context_block = str(page_context or "").strip() or "None"

    return (
        f"Translate manga dialogue from {source_language} to {target_language}.\n"
        f"Style: {style}. Use natural manga dialogue, not plain textbook English.\n"
        f"Rules:\n"
        f"- Preserve tone, emotion, and character names consistently across the whole page.\n"
        f"- If the same source name appears again, reuse the same English spelling.\n"
        f"- Japanese often omits subjects. Infer speaker and target from source pronouns, imperatives, verbs, and nearby bubbles before choosing I/you/he/she/they.\n"
        f"- If a line describes the listener's body, state, or action after a command/addressing line, prefer \"you\" over \"I\" unless the source explicitly says 俺, 僕, 私, or another first-person marker.\n"
        f"- Do not make the speaker perform an action that the source attributes to the addressed character.\n"
        f"- Preserve honorifics such as senpai, sensei, san, and chan when they show relationships.\n"
        f"- Use concise manga-style phrasing: contractions, natural reactions, and bubble-friendly rhythm.\n"
        f"- Preserve jokes/wordplay where possible.\n"
        f"- Do not merge entries; keep one translation per provided id.\n"
        f"- Treat protected entity placeholders as fixed names that must stay unchanged.\n"
        f"- If the page context suggests a block is a name, keep that block unchanged.\n"
        f"- Do not output anything outside the required JSON.\n"
        f"Glossary (protected spellings): {glossary_block}\n"
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
