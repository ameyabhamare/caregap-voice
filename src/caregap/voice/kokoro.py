"""Kokoro TTS (local, free) via kokoro-onnx: the Kokoro-82M model on ONNX Runtime."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import numpy as np

from caregap.voice.base import Audio
from caregap.voice.pronunciation import PronunciationDict

RELEASE = "https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0"
MODEL_FILES = {
    "kokoro-v1.0.onnx": "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
    "voices-v1.0.bin": "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
}
DEFAULT_VOICE = "af_heart"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_models(model_dir: Path) -> Path:
    """Download the pinned model files once and verify their checksums."""
    model_dir.mkdir(parents=True, exist_ok=True)
    for name, sha in MODEL_FILES.items():
        path = model_dir / name
        if path.exists() and _sha256(path) == sha:
            continue
        tmp = path.with_suffix(".part")
        with httpx.stream("GET", f"{RELEASE}/{name}", follow_redirects=True, timeout=600) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        if _sha256(tmp) != sha:
            tmp.unlink()
            raise RuntimeError(f"checksum mismatch for {name}")
        tmp.rename(path)
    return model_dir


class KokoroTTS:
    name = "kokoro"

    def __init__(self, model_dir: Path, *, overrides: PronunciationDict | None = None) -> None:
        from kokoro_onnx import Kokoro

        ensure_models(model_dir)
        self._kokoro = Kokoro(
            str(model_dir / "kokoro-v1.0.onnx"), str(model_dir / "voices-v1.0.bin")
        )
        self.overrides = overrides

    def voices(self) -> list[str]:
        return list(self._kokoro.get_voices())

    def phonemes(self, text: str, lang: str = "en-us") -> str:
        def g2p(chunk: str) -> str:
            return str(self._kokoro.tokenizer.phonemize(chunk, lang))

        return self.overrides.to_phonemes(text, g2p) if self.overrides else g2p(text)

    def synthesize(self, text: str, *, voice: str = DEFAULT_VOICE, lang: str = "en-us") -> Audio:
        samples, rate = self._kokoro.create(
            self.phonemes(text, lang), voice=voice, lang=lang, is_phonemes=True
        )
        return Audio(np.asarray(samples, dtype=np.float32), int(rate))
