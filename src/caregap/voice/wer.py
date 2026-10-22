"""Word error rate with alignment, so errors can be attributed to name words."""

from __future__ import annotations

import re
from dataclasses import dataclass

_APOSTROPHES = str.maketrans(
    {"\N{RIGHT SINGLE QUOTATION MARK}": "'", "\N{LEFT SINGLE QUOTATION MARK}": "'"}
)


def tokens(text: str) -> list[str]:
    """Lowercase words; punctuation and hyphens become spaces, inner apostrophes stay."""
    text = text.translate(_APOSTROPHES).lower()
    text = re.sub(r"[^a-z0-9']+", " ", text)
    return [t.strip("'") for t in text.split() if t.strip("'")]


@dataclass(frozen=True)
class Op:
    kind: str  # "C" correct, "S" substitution, "D" deletion, "I" insertion
    ref: int | None
    hyp: int | None


def align(ref: list[str], hyp: list[str]) -> list[Op]:
    """Minimum-edit alignment (Levenshtein) with a deterministic backtrace."""
    n, m = len(ref), len(hyp)
    dist = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dist[i][0] = i
    for j in range(m + 1):
        dist[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            sub = dist[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1])
            dist[i][j] = min(sub, dist[i - 1][j] + 1, dist[i][j - 1] + 1)
    ops: list[Op] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and dist[i][j] == dist[i - 1][j - 1] + (ref[i - 1] != hyp[j - 1]):
            ops.append(Op("C" if ref[i - 1] == hyp[j - 1] else "S", i - 1, j - 1))
            i, j = i - 1, j - 1
        elif i > 0 and dist[i][j] == dist[i - 1][j] + 1:
            ops.append(Op("D", i - 1, None))
            i -= 1
        else:
            ops.append(Op("I", None, j - 1))
            j -= 1
    return ops[::-1]


@dataclass(frozen=True)
class WerResult:
    ref_words: int
    substitutions: int
    deletions: int
    insertions: int

    @property
    def errors(self) -> int:
        return self.substitutions + self.deletions + self.insertions

    @property
    def wer(self) -> float:
        return self.errors / self.ref_words if self.ref_words else 0.0


def wer(reference: str, hypothesis: str) -> WerResult:
    ops = align(tokens(reference), tokens(hypothesis))
    return WerResult(
        ref_words=len(tokens(reference)),
        substitutions=sum(o.kind == "S" for o in ops),
        deletions=sum(o.kind == "D" for o in ops),
        insertions=sum(o.kind == "I" for o in ops),
    )


@dataclass(frozen=True)
class NameHit:
    name: str
    correct: bool  # every word of the name aligned as correct
    word_errors: int  # substitutions + deletions on the name's words
    heard: str  # hypothesis words aligned to the name's span


def name_hits(reference: str, hypothesis: str, names: list[str]) -> list[NameHit]:
    """Score each occurrence of each name in the reference."""
    ref, hyp = tokens(reference), tokens(hypothesis)
    ops = align(ref, hyp)
    by_ref = {o.ref: o for o in ops if o.ref is not None}
    hits: list[NameHit] = []
    for name in names:
        name_toks = tokens(name)
        for start in range(len(ref) - len(name_toks) + 1):
            if ref[start : start + len(name_toks)] != name_toks:
                continue
            span = [by_ref[k] for k in range(start, start + len(name_toks))]
            errs = sum(o.kind in ("S", "D") for o in span)
            heard = " ".join(hyp[o.hyp] for o in span if o.hyp is not None)
            hits.append(NameHit(name, errs == 0, errs, heard))
    return hits
