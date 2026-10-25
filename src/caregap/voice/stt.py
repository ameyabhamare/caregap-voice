"""Speech-to-text with faster-whisper (local, free)."""

from __future__ import annotations

import io
from pathlib import Path

from caregap.voice.base import Audio

DEFAULT_MODEL = "small.en"


class WhisperSTT:
    name = "faster-whisper"

    def __init__(self, model_size: str = DEFAULT_MODEL, *, download_root: Path) -> None:
        from faster_whisper import WhisperModel

        self.model_size = model_size
        self._model = WhisperModel(
            model_size, device="cpu", compute_type="int8", download_root=str(download_root)
        )

    def transcribe(self, audio: Audio) -> str:
        # Pass WAV bytes so faster-whisper decodes and resamples to 16 kHz properly.
        # Plain greedy-free decoding with no initial prompt: a prompt listing names would
        # bias the transcript and inflate name accuracy.
        segments, _ = self._model.transcribe(
            io.BytesIO(audio.to_wav()),
            language="en",
            beam_size=5,
            temperature=0.0,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        return " ".join(s.text.strip() for s in segments).strip()
