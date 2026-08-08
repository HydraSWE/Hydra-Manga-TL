"""Post-processing utilities for manga translation outputs."""

from __future__ import annotations

import re

from hydra_manga_tl.core.normalization import is_invalid_honorific_carrier

HONORIFIC_SUFFIX_PATTERN = (
    r"(?:san|chan|kun|sama|senpai|sensei|dono|tan|chama|shisho|"
    r"hakase|bucho|shacho|kaicho|kohai|hime|bozu|shonin|niisan|"
    r"neesan|niisama|neesama|ojisan|obasan|paii)"
)


def clean_pronoun_artifacts(text: str) -> str:
    """Clean mangled pronoun-suffix welds and title duplications from LLM outputs."""
    if not text:
        return text

    # 1. Clean duplicated honorific suffixes (e.g. Tanaka-senpai-senpai -> Tanaka-senpai)
    cleaned = re.sub(rf"(-{HONORIFIC_SUFFIX_PATTERN})\1+\b", r"\1", text, flags=re.IGNORECASE)

    # 2. Clean honorific suffixes attached to grammar words while preserving
    # valid address forms such as Mika-kun or Coach-sensei.
    def remove_invalid_carrier_suffix(match: re.Match) -> str:
        carrier = match.group("carrier")
        return carrier if is_invalid_honorific_carrier(carrier) else match.group(0)

    cleaned = re.sub(
        rf"\b(?P<carrier>[A-Za-z]+)-{HONORIFIC_SUFFIX_PATTERN}\b",
        remove_invalid_carrier_suffix,
        cleaned,
        flags=re.IGNORECASE,
    )

    return cleaned.strip()
