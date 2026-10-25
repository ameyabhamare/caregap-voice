# 6. TTS strategy: local Kokoro, pronunciation overrides, measured round trip

Date: 2026-10-22. Status: Accepted.

## Context
Outreach scripts are spoken to members, and callback answers (Phase 7) will mention drug
and plan names. TTS must be free and local by default, paid providers must be possible for
an optional quality comparison, and pronunciation of names must be measurable, not assumed.

## Decision
- **Kokoro-82M via kokoro-onnx** is the only enabled TTS. ONNX Runtime instead of PyTorch
  keeps the install small; model files are pinned by sha256 and fetched once from the
  kokoro-onnx GitHub release. Default voice `af_heart`; Spanish voices exist for Phase 6.
- **One interface** (`TTSProvider.synthesize -> Audio`). OpenAI and ElevenLabs are stubs that
  refuse unless explicitly enabled; their implementations, pricing and ledger metering
  come with the optional comparison in Phase 11.
- **Pronunciation overrides** (`voice/pronunciations.yaml`): IPA in Kokoro's phoneme set
  plus a plain respelling for text-only providers. The Kokoro path phonemizes ordinary
  text with espeak and splices the override IPA in for matched terms (whole words, longest
  match first). A test checks every IPA symbol is in Kokoro's vocabulary. espeak's default
  pronunciations of many drug names are wrong (e.g. stress on "a-" in atorvastatin).
- **Measured with a round trip**: TTS -> faster-whisper (`small.en`, int8, CPU) -> WER with
  a Levenshtein alignment so errors can be attributed to name words. Whisper runs with no
  initial prompt, which would otherwise bias it toward the names. Two sets: the 100 Phase 3
  eval scripts (overall WER) and 56 dev-time sentences covering every override term,
  synthesized with and without overrides (name accuracy before/after, by kind).
- Audio is rendered on request (`POST /scripts/{id}/audio`) for compliant or approved
  scripts only, stored under `data/audio/`, and recorded in `script_audio`. Object storage
  with signed URLs is Phase 11.

## Consequences
- Whisper's own errors are part of every WER number; `small.en` is chosen over `base.en`
  to keep that floor low. Name accuracy compares conditions on the same STT, so the
  before/after delta is meaningful even where absolute numbers are not.
- Splicing IPA phonemizes text in chunks, which can change prosody slightly at the joins.
- Outreach scripts never contain drug names (compliance forbids them), so medication
  overrides matter for Phase 7 callback answers, not for outreach.

## Update (2026-10-25, Phase 12)
- Paid TTS was not added in Phase 11 (owner-approved default). OpenAI and ElevenLabs
  remain refusing stubs, with no price in the pricing table, so they fail closed.
- Audio for live calls goes through short-lived presigned R2 URLs (ADR 0013). The local
  default still serves it from `data/audio/` through the core service.
- Spanish round-trip WER is still unmeasured: `small.en` is English-only, and a
  multilingual Whisper model (about 480 MB) did not fit the disk budget.
