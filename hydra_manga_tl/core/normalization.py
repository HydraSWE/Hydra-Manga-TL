"""Source-aware manga localization cleanup.

This module does not translate from scratch. It reads reusable cues from the
source line, then uses those cues to reshape the provider's English into manga
dialogue without storing one-off source-to-English sentence answers.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hydra_manga_tl.translation.engines.base import PageDialogue, PageTranslation


HONORIFICS = ("先輩", "先生", "さん", "くん", "君", "ちゃん", "様")
TOPIC_MARKERS = (*HONORIFICS, "は", "が", "も", "：", ":")
ILLNESS_MARKERS = ("体調不良", "風邪", "病気", "具合")
ABSENCE_MARKERS = ("休み", "休む", "欠席", "休養")
START_MARKERS = ("始める", "始めよう", "始めるぞ", "開始")
CALL_MARKERS = ("電話", "連絡")
AFTER_MARKERS = ("あと", "後")
TENTATIVE_MARKERS = ("みるか", "みよう", "かな", "か")
CARE_MARKERS = ("体調管理", "気を付け", "気をつけ")
NEGATIVE_MARKERS = ("ダメ", "駄目", "無理")

_UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")
_GLOBAL_TEXT_TRANSLATION = str.maketrans({
    "\u2018": "'",
    "\u2019": "'",
    "\u201a": "'",
    "\u201b": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u201e": '"',
    "\u201f": '"',
    "\u2026": "...",
    "\u2010": "-",
    "\u2011": "-",
    "\u2012": "-",
    "\u2013": "-",
    "\u2014": "-",
    "\u2015": "-",
    "\u00a0": " ",
    "\u200b": "",
    "\ufeff": "",
})


def normalize_global_text(text: str) -> str:
    """Normalize provider/OCR punctuation before persistence or rendering."""
    value = str(text or "")
    value = _UNICODE_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 16)), value)
    value = value.translate(_GLOBAL_TEXT_TRANSLATION)
    value = re.sub(r"[ \t\r\f\v]+", " ", value)
    value = re.sub(r"\.{4,}", "...", value)
    value = re.sub(r"\s+([,.!?;:])", r"\1", value)
    return value.strip()
BODY_MARKERS = ("身体", "体")
PLEASANT_MARKERS = ("気持ちいい", "気持ち良い")
FIRST_PERSON_MARKERS = ("俺", "オレ", "僕", "私", "あたし", "わたし")
SECOND_PERSON_MARKERS = ("お前", "君", "あなた", "あんた", "てめえ")
TARGET_BODY_ACTION_MARKERS = ("射精", "硬い", "勃起", "イキ", "イク", "いく", "感じ", "濡れ")
TARGET_DEICTIC_MARKERS = ("こんな", "そんな", "その", "まま", "なんて", "のに")
TARGET_COMMAND_MARKERS = ("くれ", "して", "しろ", "なさい", "ろよ", "てよ")


@dataclass(frozen=True)
class SourceFeatures:
    source: str
    name_key: str = ""
    subject: str = ""
    honorific: str = ""
    has_pause: bool = False
    has_stammer: bool = False
    is_question: bool = False
    is_exclamation: bool = False
    is_illness_absence: bool = False
    is_cold: bool = False
    is_repeat_or_also: bool = False
    is_today_again: bool = False
    asks_for_care: bool = False
    is_start_command: bool = False
    has_attention_call: bool = False
    is_tentative_call: bool = False
    is_after_practice: bool = False
    is_acknowledgement: bool = False
    is_negative: bool = False
    is_again: bool = False
    is_body_feeling: bool = False
    is_involuntary: bool = False
    asks_if_okay: bool = False
    target_person: str = ""


def has_all(text: str, values: tuple[str, ...]) -> bool:
    return all(value in text for value in values)


def has_any(text: str, values: tuple[str, ...]) -> bool:
    return any(value in text for value in values)


def source_name_key(text: str) -> str:
    stripped = str(text).strip()
    for marker in TOPIC_MARKERS:
        if marker in stripped:
            candidate = stripped.split(marker, 1)[0].strip()
            if 1 <= len(candidate) <= 4 and any("\u3400" <= char <= "\u9fff" for char in candidate):
                return candidate
    return ""


def source_honorific(text: str) -> str:
    for honorific in HONORIFICS:
        if honorific in text:
            return honorific
    return ""


def english_lead_name(text: str) -> str:
    match = re.match(r"\s*([A-Z][A-Za-z]+)(?:'s|\b)", str(text))
    return match.group(1) if match else ""


def english_subject(text: str) -> str:
    match = re.match(
        r"\s*(?P<subject>[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*)?)"
        r"(?:'s| is| was| has| has been|\b)",
        str(text),
    )
    return match.group("subject") if match else ""


def name_aliases(page: PageDialogue, translations: list[dict]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for source, translated in zip(page.dialogue, translations):
        key = source_name_key(str(source.get("text", "")))
        alias = english_lead_name(str(translated.get("text", "")))
        if key and alias:
            aliases.setdefault(key, alias)
    return aliases


def collapse_repeated_english(text: str, *, max_repeats: int = 3) -> str:
    def replace(match: re.Match) -> str:
        word = match.group("word")
        punct = match.group("punct") or ""
        return ", ".join([word] * max_repeats) + punct

    return re.sub(
        r"(?i)\b(?P<word>[a-z]+)(?:,\s*(?P=word)){%d,}(?P<punct>[.!?])?" % max_repeats,
        replace,
        str(text).strip(),
    )


def sentence_case(text: str) -> str:
    text = str(text).strip()
    return text[:1].upper() + text[1:] if text else text


def has_first_person_source(text: str) -> bool:
    return has_any(text, FIRST_PERSON_MARKERS)


def has_second_person_source(text: str) -> bool:
    return has_any(text, SECOND_PERSON_MARKERS)


def addresses_listener(source: str) -> bool:
    return has_second_person_source(source) or (
        has_first_person_source(source)
        and has_any(source, TARGET_COMMAND_MARKERS)
        and has_any(source, ("を", "に", "させ", "して", "くれ"))
    )


def describes_listener_body(source: str) -> bool:
    return (
        not has_first_person_source(source)
        and has_any(source, TARGET_BODY_ACTION_MARKERS)
        and has_any(source, TARGET_DEICTIC_MARKERS)
    )


def infer_target_people(page: PageDialogue) -> dict[str, str]:
    target_people: dict[str, str] = {}
    listener_context = 0
    for item in page.dialogue:
        entry_id = str(item.get("id", ""))
        source = str(item.get("text", ""))
        if has_second_person_source(source):
            target_people[entry_id] = "second"
            listener_context = 2
        elif describes_listener_body(source) and listener_context > 0:
            target_people[entry_id] = "second"
            listener_context = max(listener_context - 1, 0)
        elif describes_listener_body(source) and any(
            has_second_person_source(str(other.get("text", ""))) or addresses_listener(str(other.get("text", "")))
            for other in page.dialogue
        ):
            target_people[entry_id] = "second"
        if addresses_listener(source):
            listener_context = 2
        elif listener_context > 0:
            listener_context -= 1
    return target_people


def extract_source_features(
    source: str, translated: str, aliases: dict[str, str], *, target_person: str = "",
) -> SourceFeatures:
    source = str(source).strip()
    text = str(translated).strip()
    name_key = source_name_key(source)
    subject = aliases.get(name_key, "") or english_subject(text)
    if not subject and "コーチ" in source:
        subject = "Coach"
    illness_absence = has_any(source, ILLNESS_MARKERS) and has_any(source, ABSENCE_MARKERS)
    return SourceFeatures(
        source=source,
        name_key=name_key,
        subject=subject,
        honorific=source_honorific(source),
        has_pause=source.rstrip().endswith(("…", "...")),
        has_stammer=bool(re.search(r"[ぁ-んァ-ンA-Za-z]([…・.：:])\s*[ぁ-んァ-ンA-Za-z]", source)),
        is_question=source.rstrip().endswith(("？", "?")) or "かな" in source,
        is_exclamation=source.rstrip().endswith(("！", "!")) or "ッ" in source,
        is_illness_absence=illness_absence,
        is_cold="風邪" in source,
        is_repeat_or_also="も" in source,
        is_today_again="今日も" in source,
        asks_for_care=has_any(source, CARE_MARKERS),
        is_start_command=has_any(source, START_MARKERS),
        has_attention_call=has_any(source, ("おーい", "おい", "ねえ", "なあ")),
        is_tentative_call=has_any(source, CALL_MARKERS) and has_any(source, AFTER_MARKERS),
        is_after_practice="練習" in source and has_any(source, AFTER_MARKERS),
        is_acknowledgement="はい" in source,
        is_negative=has_any(source, NEGATIVE_MARKERS),
        is_again="また" in source,
        is_body_feeling=has_any(source, BODY_MARKERS) and has_any(source, PLEASANT_MARKERS),
        is_involuntary="しまう" in source,
        asks_if_okay="大丈夫" in source,
        target_person=target_person,
    )


class MangaStyleRewriter:
    def __init__(self, features: SourceFeatures, text: str) -> None:
        self.features = features
        self.text = str(text).strip()

    def rewrite(self) -> str:
        self.text = html_unescape_punctuation(self.text)
        self.text = collapse_repeated_english(self.text)
        self._use_contractions()
        self._localize_illness_absence()
        self._localize_start_command()
        self._localize_tentative_call()
        self._localize_acknowledgement()
        self._localize_negative()
        self._localize_body_feeling()
        self._repair_pronoun_perspective()
        self._preserve_honorific_name()
        self._match_source_punctuation()
        return sentence_case(self.text)

    def _use_contractions(self) -> None:
        replacements = (
            (r"\bI am\b", "I'm"),
            (r"\byou are\b", "you're"),
            (r"\bhe is\b", "he's"),
            (r"\bshe is\b", "she's"),
            (r"\bit is\b", "it's"),
            (r"\bthat is\b", "that's"),
            (r"\bdo not\b", "don't"),
            (r"\bcannot\b", "can't"),
            (r"\bwill not\b", "won't"),
            (r"\blet us\b", "let's"),
        )
        for pattern, replacement in replacements:
            self.text = re.sub(pattern, replacement, self.text, flags=re.IGNORECASE)

    def _localize_illness_absence(self) -> None:
        if not self.features.is_illness_absence:
            return
        if not re.search(r"\b(sick|cold|unwell|ill|absent|off|out|not feeling well)\b", self.text, re.IGNORECASE):
            return
        subject = self.features.subject
        if not subject:
            return
        condition = "with a cold" if self.features.is_cold or re.search(r"\bcold\b", self.text, re.IGNORECASE) else "sick"
        relation = "is" if subject == "Coach" else "'s"
        phrasing = f"{subject} {relation} out {condition}".replace(" 's", "'s")
        if self.features.is_today_again:
            phrasing += " again today"
        elif self.features.is_repeat_or_also and re.search(r"\btoo|also|again\b", self.text, re.IGNORECASE):
            phrasing += " too"
        if self.features.asks_for_care and re.search(r"\b(care|look after|take care|watch)\b", self.text, re.IGNORECASE):
            care_tail = re.sub(r"^.*?\b(so|then|please)?\s*", "", self.text, flags=re.IGNORECASE).strip()
            phrasing = f"{phrasing}... {care_tail}" if care_tail else phrasing
        self.text = phrasing

    def _localize_start_command(self) -> None:
        if not self.features.is_start_command:
            return
        if not re.search(r"\b(start|begin|get started|kick off)\b", self.text, re.IGNORECASE):
            return
        self.text = re.sub(r"(?i)^.*?\b(?:start|begin|get started|kick off)\b.*$", "let's get started", self.text)
        if self.features.has_attention_call:
            self.text = "All right, " + self.text

    def _localize_tentative_call(self) -> None:
        if not self.features.is_tentative_call or not re.search(r"\bcall|phone|contact\b", self.text, re.IGNORECASE):
            return
        self.text = re.sub(r"(?i)\blet's\s+call\b", "I'll call", self.text)
        if self.features.is_after_practice and not re.search(r"\bafter practice\b", self.text, re.IGNORECASE):
            self.text = self.text.rstrip(".!?") + " after practice"
        if has_any(self.features.source, TENTATIVE_MARKERS) and not re.search(r"\b(maybe|might|try)\b", self.text, re.IGNORECASE):
            softened = self.text[:1].lower() + self.text[1:]
            softened = re.sub(r"\bi\b", "I", softened)
            self.text = "Maybe " + softened

    def _localize_acknowledgement(self) -> None:
        if not self.features.is_acknowledgement or not re.search(r"\byes|okay|understood\b", self.text, re.IGNORECASE):
            return
        self.text = re.sub(r"(?i)^.*?\b(yes|okay|understood)\b.*$", r"\1", self.text).strip()
        if re.search(r"\bsir\b", self.text, re.IGNORECASE):
            self.text = re.sub(r"(?i)\bsir\b", "sir", self.text)
        elif re.search(r"\bsir\b", self.features.source, re.IGNORECASE):
            self.text += ", sir"
        if self.features.has_stammer:
            self.text = "Ah... " + self.text[:1].lower() + self.text[1:]

    def _localize_negative(self) -> None:
        if not self.features.is_negative:
            return
        self.text = collapse_repeated_english(self.text, max_repeats=2)
        if self.features.is_again and re.search(r"\b(no|again)\b", self.text, re.IGNORECASE):
            self.text = self.text.rstrip(".!?")
            self.text = re.sub(r"(?i)\bno,\s*no\b", "no", self.text)
            if "again" not in self.text.lower():
                self.text += "... not again"
        if re.search(r"\bno\b", self.text, re.IGNORECASE) and self.features.has_stammer:
            self.text = re.sub(r"(?i)\bno\b", "N-no", self.text, count=1)

    def _localize_body_feeling(self) -> None:
        if not self.features.is_body_feeling:
            return
        self.text = re.sub(
            r"(?i)\bI\s+feel like I(?:'m| am) feeling good\b",
            "my body... it's starting to feel good",
            self.text,
        )
        self.text = re.sub(r"(?i)\bfeel like I(?:'m| am) feeling good\b", "starting to feel good", self.text)
        if self.features.is_involuntary and "despite myself" not in self.text.lower():
            self.text = self.text.rstrip(".!?") + " despite myself"

    def _repair_pronoun_perspective(self) -> None:
        if self.features.target_person != "second" or has_first_person_source(self.features.source):
            return
        if not has_any(self.features.source, TARGET_BODY_ACTION_MARKERS):
            return
        text = self.text
        text = re.sub(r"(?i)\bI(?:'ve| have)\s+already\b", "you've already", text)
        text = re.sub(r"(?i)\bI(?:'m| am)\s+still\b", "you're still", text)
        text = re.sub(r"(?i)\bI(?:'m| am)\s+so\b", "you're so", text)
        text = re.sub(r"(?i)\bmy\s+(body|body's|desire|urges|arousal)\b", r"your \1", text)
        text = re.sub(r"(?i)\byou've already ([^.!?;,]+)\s+and\s+still\b", r"you've already \1 and you're still", text)
        text = re.sub(r"(?i)\byou've already ([^.!?;,]+),\s+and you're\b", r"you've already \1, and you're", text)
        self.text = text

    def _preserve_honorific_name(self) -> None:
        if not (self.features.subject and self.features.honorific):
            return
        romanized = {
            "先輩": "senpai",
            "先生": "sensei",
            "さん": "san",
            "くん": "kun",
            "君": "kun",
            "ちゃん": "chan",
            "様": "sama",
        }[self.features.honorific]
        styled = f"{self.features.subject}-{romanized}"
        if self.features.asks_if_okay and re.search(r"\b(okay|alright|all right|fine)\b", self.text, re.IGNORECASE):
            self.text = re.sub(r"(?i)^[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*)?", styled, self.text)
            if not self.text.lower().startswith(("is ", "are ", "will ", "can ", "does ", "do ")):
                self.text = f"Is {styled} okay"
            return
        self.text = re.sub(r"^[A-Z][A-Za-z]*(?:\s+[A-Z][A-Za-z]*)?\b", styled, self.text)

    def _match_source_punctuation(self) -> None:
        self.text = self.text.strip()
        if not self.text:
            return
        if self.features.has_pause:
            self.text = re.sub(r"[.!?。！？]+$", "", self.text) + "..."
            return
        if self.features.is_question:
            self.text = re.sub(r"[.!?。！？]+$", "", self.text) + "?"
            return
        if self.features.is_exclamation or self.features.is_negative or self.features.is_acknowledgement:
            if self.text.endswith(("!!", "！！")):
                return
            if self.text.endswith(("!", "！")):
                self.text += "!"
                return
            self.text = re.sub(r"[.!?。！？]+$", "", self.text) + "!"
            return
        if not self.text.endswith((".", "!", "?", "...")):
            self.text += "."


def html_unescape_punctuation(text: str) -> str:
    return re.sub(r"\s+([,.!?])", r"\1", str(text).strip())


def normalize_dialogue_dashes(text: str) -> str:
    text = _UNICODE_ESCAPE_RE.sub(lambda match: chr(int(match.group(1), 16)), str(text))
    text = re.sub(r"\s*[—–]\s*", "... ", text)
    return normalize_global_text(re.sub(r"\.{3,}\s+", "... ", text))


def normalize_manga_line(
    original: str, translated: str, aliases: dict[str, str], *, target_person: str = "",
) -> str:
    features = extract_source_features(original, translated, aliases, target_person=target_person)
    return normalize_dialogue_dashes(MangaStyleRewriter(features, translated).rewrite())


def normalize_page_translation(page: PageDialogue, result: PageTranslation) -> PageTranslation:
    from hydra_manga_tl.translation.engines.base import PageTranslation

    translations = [dict(item) for item in result.translations]
    aliases = name_aliases(page, translations)
    by_id = {str(item.get("id")): str(item.get("text", "")) for item in page.dialogue}
    target_people = infer_target_people(page)
    normalized = [
        {
            **item,
            "text": normalize_manga_line(
                by_id.get(str(item.get("id")), ""),
                str(item.get("text", "")),
                aliases,
                target_person=target_people.get(str(item.get("id")), ""),
            ),
        }
        for item in translations
    ]
    return PageTranslation(
        source_language=result.source_language,
        target_language=result.target_language,
        translations=normalized,
    )
