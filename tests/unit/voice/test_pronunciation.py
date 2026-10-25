from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

import pytest

from caregap.voice.pronunciation import (
    IPA_VOWELS,
    STRESS_MARKS,
    Pronunciation,
    PronunciationDict,
)

D = PronunciationDict.load()


def _vocab() -> set[str]:
    config = json.loads(files("kokoro_onnx").joinpath("config.json").read_text())
    return set(config["vocab"])


def test_every_ipa_symbol_is_in_kokoros_vocabulary() -> None:
    vocab = _vocab() | {" "}
    for e in D.entries.values():
        assert set(e.ipa) <= vocab, (e.term, set(e.ipa) - vocab)


def test_covers_both_kinds() -> None:
    assert len(D.terms("medication")) >= 20
    assert {"Sunrise Medicare Advantage", "Horizon ACA Marketplace"} <= set(D.terms("plan"))


def test_find_prefers_longest_and_matches_whole_words_case_insensitively() -> None:
    text = "Sunrise Medicare Advantage covers ATORVASTATIN, not atorvastatins or Medicare-ish."
    assert [e.term for e in D.find(text)] == ["Sunrise Medicare Advantage", "atorvastatin"]


def test_to_phonemes_splices_ipa_between_g2p_chunks() -> None:
    def fake_g2p(chunk: str) -> str:
        return f"<{chunk}>"

    out = D.to_phonemes("Take metformin daily with Sunrise Medicare Advantage.", fake_g2p)
    assert out == (
        f"<Take> {D.entries['metformin'].ipa} <daily with> "
        f"{D.entries['sunrise medicare advantage'].ipa} <.>"
    )


def test_text_without_terms_is_phonemized_whole() -> None:
    assert D.to_phonemes("Hello there.", lambda c: f"<{c}>") == "<Hello there.>"


def test_respell() -> None:
    assert D.respell("Your lisinopril refill") == "Your lye-SIN-oh-pril refill"


def test_duplicate_terms_rejected() -> None:
    e = Pronunciation(term="x", kind="medication", ipa="ɛks", respelling="ex")
    with pytest.raises(ValueError, match="duplicate"):
        PronunciationDict([e, e.model_copy(update={"term": "X"})])


def test_name_sentences_cover_every_term_and_contain_their_names() -> None:
    import yaml

    from caregap.voice.wer import tokens

    data = yaml.safe_load(Path("fixtures/voice/name_sentences.yaml").read_text())
    covered = {n.lower() for s in data["sentences"] for n in s["names"]}
    assert covered == set(D.entries)
    for s in data["sentences"]:
        for name in s["names"]:
            joined = " ".join(tokens(s["text"]))
            assert " ".join(tokens(name)) in joined, (name, s["text"])


def test_stress_marks_sit_directly_before_a_vowel_like_espeak() -> None:
    # Kokoro learned espeak's convention (stress mark before the vowel). Marks before a
    # consonant made it insert stray vowels ("a torvastatin") in the first voice eval.
    for e in D.entries.values():
        for i, ch in enumerate(e.ipa):
            if ch in STRESS_MARKS:
                assert i + 1 < len(e.ipa) and e.ipa[i + 1] in IPA_VOWELS, (e.term, e.ipa)
