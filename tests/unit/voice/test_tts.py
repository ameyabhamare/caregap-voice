from __future__ import annotations

import io
import wave
from pathlib import Path

import numpy as np
import pytest

from caregap.voice.base import Audio, TTSDisabledError
from caregap.voice.paid import ElevenLabsTTS, OpenAITTS


def test_audio_to_wav_round_trip() -> None:
    samples = np.sin(np.linspace(0, 100, 24000)).astype(np.float32)
    audio = Audio(samples, 24000)
    assert audio.duration_s == pytest.approx(1.0)
    with wave.open(io.BytesIO(audio.to_wav())) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (
            1, 2, 24000, 24000,
        )  # fmt: skip


def test_audio_clips_out_of_range_samples() -> None:
    wav = Audio(np.array([2.0, -2.0], dtype=np.float32), 8000).to_wav()
    frames = np.frombuffer(wav[-4:], dtype="<i2")
    assert list(frames) == [32767, -32767]


@pytest.mark.parametrize("provider", [OpenAITTS, ElevenLabsTTS])
def test_paid_providers_are_disabled_by_default(provider: type[OpenAITTS]) -> None:
    with pytest.raises(TTSDisabledError, match="disabled by default"):
        provider().synthesize("hi", voice="any")
    with pytest.raises(NotImplementedError, match="Phase 11"):
        provider(enabled=True).synthesize("hi", voice="any")


MODEL_DIR = Path("data/cache/kokoro")


@pytest.mark.models
def test_kokoro_synthesizes_with_and_without_overrides() -> None:
    if not (MODEL_DIR / "kokoro-v1.0.onnx").exists():
        pytest.skip("Kokoro model files not downloaded")
    from caregap.voice.kokoro import KokoroTTS
    from caregap.voice.pronunciation import PronunciationDict

    overrides = PronunciationDict.load()
    plain = KokoroTTS(MODEL_DIR)
    tuned = KokoroTTS(MODEL_DIR, overrides=overrides)
    text = "Your lisinopril refill is ready."
    assert plain.phonemes(text) != tuned.phonemes(text)
    assert overrides.entries["lisinopril"].ipa in tuned.phonemes(text)
    audio = tuned.synthesize(text)
    assert audio.sample_rate == 24000
    assert 1.0 < audio.duration_s < 5.0


@pytest.mark.models
def test_kokoro_speaks_spanish_without_english_overrides() -> None:
    if not (MODEL_DIR / "kokoro-v1.0.onnx").exists():
        pytest.skip("Kokoro model files not downloaded")
    from caregap.voice.kokoro import KokoroTTS
    from caregap.voice.pronunciation import PronunciationDict

    overrides = PronunciationDict.load()
    tuned = KokoroTTS(MODEL_DIR, overrides=overrides)
    text = "Su plan Sunrise Medicare Advantage le recuerda su chequeo anual."
    # English override IPA is not spliced into Spanish phonemes.
    assert tuned.phonemes(text, "es") == KokoroTTS(MODEL_DIR).phonemes(text, "es")
    assert tuned.phonemes(text, "es") != tuned.phonemes(text, "en-us")
    audio = tuned.synthesize(text, voice="ef_dora", lang="es")
    assert 2.0 < audio.duration_s < 10.0
