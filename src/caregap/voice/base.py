"""TTS interface and the audio container shared by providers."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from numpy.typing import NDArray


class TTSDisabledError(RuntimeError):
    """A paid provider was used without being explicitly enabled."""


@dataclass(frozen=True)
class Audio:
    samples: NDArray[np.float32]  # mono, -1.0..1.0
    sample_rate: int

    @property
    def duration_s(self) -> float:
        return len(self.samples) / self.sample_rate

    def to_wav(self) -> bytes:
        """16-bit PCM mono WAV."""
        pcm = (np.clip(self.samples, -1.0, 1.0) * 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(self.sample_rate)
            w.writeframes(pcm.tobytes())
        return buf.getvalue()


class TTSProvider(Protocol):
    name: str

    def synthesize(self, text: str, *, voice: str, lang: str = "en-us") -> Audio: ...
