"""Reading level of English scripts: Flesch-Kincaid grade, computed, not judged.

Syllables are counted with a vowel-group heuristic (silent final "e", "-le" endings),
accurate enough to tell grade 5 from grade 10. Spanish scripts are not graded (the
formula is English-specific).
"""

from __future__ import annotations

import re

#: Plain-language target for member communications.
MAX_GRADE = 8.0

_WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_SENTENCE_END = re.compile(r"[.!?]+(?:\s|$)")


def syllables(word: str) -> int:
    w = word.lower().replace("'", "")
    if len(w) <= 3:
        return 1
    if w.endswith("e") and not w.endswith(("le", "ee", "ye")):
        w = w[:-1]
    count = len(re.findall(r"[aeiouy]+", w))
    return max(1, count)


def fk_grade(text: str) -> float:
    """Flesch-Kincaid grade level: 0.39 * words/sentence + 11.8 * syllables/word - 15.59."""
    words = _WORD.findall(text)
    if not words:
        return 0.0
    sentences = max(1, len(_SENTENCE_END.findall(text.strip() + " ")))
    syl = sum(syllables(w) for w in words)
    return round(0.39 * len(words) / sentences + 11.8 * syl / len(words) - 15.59, 1)


def reading_level_ok(text: str, language: str) -> bool | None:
    """True if at or below ``MAX_GRADE``; None for languages the formula does not cover."""
    return fk_grade(text) <= MAX_GRADE if language == "en" else None
