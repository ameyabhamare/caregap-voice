"""Paid TTS providers: stubs behind the TTS interface, disabled by default.

They exist so the interface and configuration are settled now. Real implementations,
pricing and cost-ledger metering come with the optional quality comparison in Phase 11.
"""

from __future__ import annotations

from caregap.voice.base import Audio, TTSDisabledError


class _PaidStub:
    name = "paid"

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled

    def synthesize(self, text: str, *, voice: str, lang: str = "en-us") -> Audio:
        if not self.enabled:
            raise TTSDisabledError(
                f"{self.name} TTS is a paid provider and disabled by default "
                "(optional quality comparison, Phase 11)"
            )
        raise NotImplementedError(f"{self.name} TTS is implemented in Phase 11")


class OpenAITTS(_PaidStub):
    name = "openai"


class ElevenLabsTTS(_PaidStub):
    name = "elevenlabs"
