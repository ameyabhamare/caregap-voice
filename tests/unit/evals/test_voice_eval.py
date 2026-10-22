from __future__ import annotations

import numpy as np
import pytest

from caregap.evals.voice import report_markdown, run_names, run_scripts, summarize
from caregap.voice.base import Audio


class TextTTS:
    """Encodes the text into the samples so FakeSTT can 'hear' it back."""

    def __init__(self, tuned: bool) -> None:
        self.name = "text"
        self.tuned = tuned
        self.spoken: dict[int, str] = {}

    def synthesize(self, text: str, *, voice: str, lang: str = "en-us") -> Audio:
        samples = np.full(len(text) + 1, len(self.spoken) / 1e6, dtype=np.float32)
        self.spoken[len(self.spoken)] = text
        return Audio(samples, 24000) if not self.tuned else Audio(-samples - 1e-7, 24000)


class FakeSTT:
    """Mishears 'lisinopril' unless the tuned TTS spoke it."""

    def __init__(self, plain: TextTTS, tuned: TextTTS) -> None:
        self.plain, self.tuned = plain, tuned

    def transcribe(self, audio: Audio) -> str:
        tuned = audio.samples[0] < 0
        key = round(abs(float(audio.samples[0])) * 1e6 - (1e-7 * 1e6 if tuned else 0))
        text = (self.tuned if tuned else self.plain).spoken[key]
        return text if tuned else text.replace("lisinopril", "lice in a pill")


SENTENCES = [
    {"text": "Your plan covers lisinopril.", "names": ["lisinopril"], "kind": "medication"},
    {"text": "Welcome to Sunrise Medicare Advantage.", "names": ["Sunrise Medicare Advantage"],
     "kind": "plan"},
]  # fmt: skip


def test_names_before_after_and_summary() -> None:
    plain, tuned = TextTTS(False), TextTTS(True)
    stt = FakeSTT(plain, tuned)
    rows = run_names(SENTENCES, {"plain": plain, "overrides": tuned}, stt, "v", lambda _: None)
    m = summarize([], rows)
    assert m["names_plain_medication_accuracy"] == 0.0
    assert m["names_overrides_medication_accuracy"] == 1.0
    assert m["names_medication_accuracy_delta"] == 1.0
    assert m["names_plain_plan_accuracy"] == m["names_overrides_plan_accuracy"] == 1.0
    assert m["names_plain_all_accuracy"] == 0.5
    assert m["names_plain_medication_word_error_rate"] == 1.0

    report = report_markdown(m, rows, {"tts": "t", "voice": "v", "stt": "s", "scripts_run": "r"})
    assert "| medication | 0.0% | 100.0% |" in report
    assert "✗ lice in a pill" in report


def test_script_wer_is_corpus_level() -> None:
    tts = TextTTS(False)
    stt = FakeSTT(tts, TextTTS(True))
    rows = run_scripts(["take lisinopril daily", "hello there"], tts, stt, "v", lambda _: None)
    m = summarize(rows, [])
    # "take lice in a pill daily": lisinopril->lice (S) + 3 insertions = 4 errors / 5 words
    assert m["scripts_wer"] == pytest.approx(4 / 5)
    assert m["scripts_perfect_rate"] == 0.5
