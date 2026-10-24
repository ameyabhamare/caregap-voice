"""Render outreach scripts to audio files and record them."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from caregap import audit, observability
from caregap.voice.base import TTSProvider

RENDERABLE = frozenset({"compliant", "approved"})


class NotRenderableError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenderedAudio:
    id: int
    script_id: int
    provider: str
    voice: str
    pronunciation_overrides: bool
    path: str
    duration_ms: int
    latency_ms: int


class AudioRenderer:
    def __init__(
        self,
        conninfo: str,
        tts: TTSProvider,
        audio_dir: Path,
        *,
        overrides_enabled: bool,
    ) -> None:
        self.conninfo = conninfo
        self.tts = tts
        self.audio_dir = audio_dir
        self.overrides_enabled = overrides_enabled

    def render(self, script: dict[str, Any], *, voice: str, lang: str) -> RenderedAudio:
        """Synthesize ``script`` with ``voice`` (espeak language ``lang``). Pronunciation
        overrides are English, so they only apply to English audio."""
        if script["status"] not in RENDERABLE:
            raise NotRenderableError(
                f"script {script['id']} is {script['status']}; only compliant or approved "
                "scripts are rendered"
            )
        tags = {"operation": "render", "customer": script.get("customer"),
                "member_id": script["member_id"], "script_id": script["id"]}  # fmt: skip
        with observability.span("tts", "TOOL", root=True, tags=tags) as span:
            span.set_inputs({"script_id": script["id"], "voice": voice, "lang": lang})
            started = time.monotonic()
            audio = self.tts.synthesize(script["script"], voice=voice, lang=lang)
            latency_ms = round((time.monotonic() - started) * 1000)
            span.set_attribute("duration_ms", round(audio.duration_s * 1000))
            trace_id = observability.trace_id()
        overrides = self.overrides_enabled and lang.startswith("en")
        self.audio_dir.mkdir(parents=True, exist_ok=True)
        path = self.audio_dir / f"script-{script['id']}-{int(time.time() * 1000)}.wav"
        path.write_bytes(audio.to_wav())
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            row = conn.execute(
                """
                INSERT INTO script_audio (script_id, provider, voice, pronunciation_overrides,
                                          path, duration_ms, latency_ms, trace_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING *
                """,
                (script["id"], self.tts.name, voice, overrides, str(path),
                 round(audio.duration_s * 1000), latency_ms, trace_id),
            ).fetchone()  # fmt: skip
            assert row is not None  # noqa: S101 - INSERT ... RETURNING always returns a row
            audit.record(
                conn, "audio_rendered", customer=script.get("customer"),
                member_id=script["member_id"], script_id=script["id"], audio_id=row["id"],
                trace_id=trace_id,
                payload={"provider": self.tts.name, "voice": voice, "lang": lang,
                         "pronunciation_overrides": overrides,
                         "duration_ms": row["duration_ms"], "latency_ms": latency_ms},
            )  # fmt: skip
        return RenderedAudio(**{k: row[k] for k in RenderedAudio.__dataclass_fields__})


def latest_audio(conninfo: str, script_id: int) -> RenderedAudio | None:
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        row = conn.execute(
            "SELECT * FROM script_audio WHERE script_id = %s ORDER BY created_at DESC, id DESC"
            " LIMIT 1",
            (script_id,),
        ).fetchone()
    if row is None:
        return None
    return RenderedAudio(**{k: row[k] for k in RenderedAudio.__dataclass_fields__})


def build_tts(data_dir: Path, *, overrides: bool) -> TTSProvider:
    from caregap.voice.kokoro import KokoroTTS
    from caregap.voice.pronunciation import PronunciationDict

    return KokoroTTS(
        data_dir / "cache" / "kokoro", overrides=PronunciationDict.load() if overrides else None
    )
