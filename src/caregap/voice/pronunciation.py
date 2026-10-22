"""Pronunciation overrides for drug and plan names.

Each entry gives an IPA pronunciation in Kokoro's phoneme set (for providers that accept
phonemes) and a plain respelling (for providers that only take text). ``to_phonemes``
phonemizes ordinary text with the provider's own G2P and splices the IPA in for matched
terms, matching whole words case-insensitively and preferring the longest term.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict

DEFAULT_PATH = Path("voice/pronunciations.yaml")


class Pronunciation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    term: str
    kind: Literal["medication", "plan"]
    ipa: str
    respelling: str


class PronunciationDict:
    def __init__(self, entries: list[Pronunciation]) -> None:
        self.entries = {e.term.lower(): e for e in entries}
        if len(self.entries) != len(entries):
            raise ValueError("duplicate pronunciation terms")
        terms = sorted(self.entries, key=len, reverse=True)
        self._pattern = re.compile(
            r"(?<![\w-])(" + "|".join(re.escape(t) for t in terms) + r")(?![\w-])", re.IGNORECASE
        ) if terms else None  # fmt: skip

    @classmethod
    def load(cls, path: Path = DEFAULT_PATH) -> PronunciationDict:
        data = yaml.safe_load(path.read_text())
        return cls([Pronunciation.model_validate(e) for e in data["entries"]])

    def terms(self, kind: str | None = None) -> list[str]:
        return [e.term for e in self.entries.values() if kind is None or e.kind == kind]

    def find(self, text: str) -> list[Pronunciation]:
        if self._pattern is None:
            return []
        return [self.entries[m.group(1).lower()] for m in self._pattern.finditer(text)]

    def respell(self, text: str) -> str:
        if self._pattern is None:
            return text
        return self._pattern.sub(lambda m: self.entries[m.group(1).lower()].respelling, text)

    def to_phonemes(self, text: str, phonemize: Callable[[str], str]) -> str:
        """Phonemes for ``text`` with override terms replaced by their IPA."""
        if self._pattern is None:
            return phonemize(text)
        parts: list[str] = []
        last = 0
        for m in self._pattern.finditer(text):
            if before := text[last : m.start()].strip():
                parts.append(phonemize(before))
            parts.append(self.entries[m.group(1).lower()].ipa)
            last = m.end()
        if rest := text[last:].strip():
            parts.append(phonemize(rest))
        return " ".join(p for p in parts if p)
