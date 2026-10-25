"""Round-trip voice eval: script -> TTS -> faster-whisper -> WER, logged to MLflow.

    make eval-voice

Set A (overall WER): the final scripts of a rule-eval MLflow run (default: the latest),
synthesized with the production setting (pronunciation overrides on).
Set B (names): fixtures/voice/name_sentences.yaml, synthesized with and without the
pronunciation overrides; reports name accuracy and name word-error rate by kind
(medication, plan) before and after.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import yaml

from caregap.voice.base import Audio, TTSProvider
from caregap.voice.wer import NameHit, name_hits, wer

EXPERIMENT = "voice-evals"
NAME_SENTENCES = Path("fixtures/voice/name_sentences.yaml")
CONDITIONS = ("plain", "overrides")


class STT(Protocol):
    def transcribe(self, audio: Audio) -> str: ...


@dataclass(frozen=True)
class ScriptRow:
    reference: str
    transcript: str
    ref_words: int
    errors: int


@dataclass(frozen=True)
class NameRow:
    condition: str
    kind: str
    reference: str
    transcript: str
    ref_words: int
    errors: int
    hits: tuple[NameHit, ...]


def run_scripts(
    scripts: list[str], tts: TTSProvider, stt: STT, voice: str, log: Callable[[str], None]
) -> list[ScriptRow]:
    rows = []
    for i, text in enumerate(scripts, 1):
        heard = stt.transcribe(tts.synthesize(text, voice=voice))
        r = wer(text, heard)
        rows.append(ScriptRow(text, heard, r.ref_words, r.errors))
        if i % 10 == 0 or i == len(scripts):
            log(f"  scripts {i}/{len(scripts)}")
    return rows


def run_names(
    sentences: list[dict[str, Any]],
    tts_by_condition: dict[str, TTSProvider],
    stt: STT,
    voice: str,
    log: Callable[[str], None],
) -> list[NameRow]:
    rows = []
    for condition in CONDITIONS:
        tts = tts_by_condition[condition]
        for s in sentences:
            heard = stt.transcribe(tts.synthesize(s["text"], voice=voice))
            r = wer(s["text"], heard)
            hits = tuple(name_hits(s["text"], heard, s["names"]))
            rows.append(
                NameRow(condition, s["kind"], s["text"], heard, r.ref_words, r.errors, hits)
            )
        log(f"  names: {condition} done")
    return rows


def summarize(scripts: list[ScriptRow], names: list[NameRow]) -> dict[str, float]:
    m: dict[str, float] = {}
    if scripts:
        m["scripts_n"] = len(scripts)
        m["scripts_wer"] = sum(r.errors for r in scripts) / sum(r.ref_words for r in scripts)
        m["scripts_perfect_rate"] = sum(r.errors == 0 for r in scripts) / len(scripts)
    for condition in CONDITIONS:
        for kind in ("medication", "plan", "all"):
            rows = [r for r in names if r.condition == condition and kind in (r.kind, "all")]
            hits = [h for r in rows for h in r.hits]
            if not hits:
                continue
            name_words = sum(len(h.name.split()) for h in hits)
            key = f"names_{condition}_{kind}"
            m[f"{key}_accuracy"] = sum(h.correct for h in hits) / len(hits)
            m[f"{key}_word_error_rate"] = sum(h.word_errors for h in hits) / name_words
            m[f"{key}_sentence_wer"] = sum(r.errors for r in rows) / sum(r.ref_words for r in rows)
    for kind in ("medication", "plan", "all"):
        before = m.get(f"names_plain_{kind}_accuracy")
        after = m.get(f"names_overrides_{kind}_accuracy")
        if before is not None and after is not None:
            m[f"names_{kind}_accuracy_delta"] = after - before
    return m


def _csv(header: list[str], rows: list[list[Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return buf.getvalue()


def report_markdown(metrics: dict[str, float], names: list[NameRow], params: dict[str, Any]) -> str:
    def pct(key: str) -> str:
        return f"{metrics[key]:.1%}" if key in metrics else "n/a"

    lines = [
        "# Voice round-trip report",
        "",
        f"TTS `{params['tts']}` voice `{params['voice']}`, STT `{params['stt']}`, "
        f"scripts from MLflow run `{params['scripts_run']}`.",
        "",
        "## Set A: outreach scripts (overrides on)",
        "",
        f"- Scripts: {int(metrics.get('scripts_n', 0))}",
        f"- Corpus WER: {pct('scripts_wer')}",
        f"- Scripts transcribed perfectly: {pct('scripts_perfect_rate')}",
        "",
        "## Set B: drug and plan names",
        "",
        "| Kind | Name accuracy (plain) | Name accuracy (overrides) | Name word errors (plain)"
        " | Name word errors (overrides) |",
        "|---|---|---|---|---|",
    ]
    for kind in ("medication", "plan", "all"):
        lines.append(
            f"| {kind} | {pct(f'names_plain_{kind}_accuracy')} "
            f"| {pct(f'names_overrides_{kind}_accuracy')} "
            f"| {pct(f'names_plain_{kind}_word_error_rate')} "
            f"| {pct(f'names_overrides_{kind}_word_error_rate')} |"
        )
    lines += [
        "",
        "## Per-name results",
        "",
        "| Name | Plain heard | Overrides heard |",
        "|---|---|---|",
    ]
    heard: dict[str, dict[str, list[str]]] = {}
    for r in names:
        for h in r.hits:
            heard.setdefault(h.name, {c: [] for c in CONDITIONS})[r.condition].append(
                ("✓ " if h.correct else "✗ ") + (h.heard or "(nothing)")
            )
    for name, by in heard.items():
        lines.append(f"| {name} | {'; '.join(by['plain'])} | {'; '.join(by['overrides'])} |")
    return "\n".join(lines) + "\n"


def load_scripts(tracking_uri: str, run_id: str | None) -> tuple[str, list[str]]:
    import mlflow

    mlflow.set_tracking_uri(tracking_uri)
    if run_id is None:
        runs = mlflow.search_runs(
            experiment_names=["rule-evals"], order_by=["start_time DESC"], max_results=1,
            filter_string="params.n_requested = '100'", output_format="list",
        )  # fmt: skip
        if not runs:
            raise SystemExit("no 100-member rule-eval run found; run `make eval-rules` first")
        run_id = runs[0].info.run_id
    text = mlflow.artifacts.load_text(f"runs:/{run_id}/results.csv")
    return run_id, [r["script"] for r in csv.DictReader(io.StringIO(text))]


def main(argv: list[str] | None = None) -> None:
    import mlflow

    from caregap.config import get_settings
    from caregap.voice.render import build_tts
    from caregap.voice.stt import WhisperSTT

    parser = argparse.ArgumentParser(prog="python -m caregap.evals.voice")
    parser.add_argument("--scripts-run", help="rule-eval MLflow run id (default: latest)")
    parser.add_argument("--limit", type=int, help="only the first N scripts (smoke runs)")
    parser.add_argument(
        "--names-only", action="store_true", help="skip set A (e.g. after a dictionary change)"
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    run_id, scripts = load_scripts(settings.mlflow_tracking_uri, args.scripts_run)
    scripts = scripts[: args.limit] if args.limit else scripts
    sentences = yaml.safe_load(NAME_SENTENCES.read_text())["sentences"]
    if args.limit:
        sentences = sentences[: args.limit]

    tts = {c: build_tts(settings.data_dir, overrides=c == "overrides") for c in CONDITIONS}
    stt = WhisperSTT(settings.stt_model, download_root=settings.data_dir / "cache" / "whisper")
    voice = settings.tts_voice
    print(f"[eval-voice] {len(scripts)} scripts (run {run_id}), {len(sentences)} name sentences")

    script_rows = (
        [] if args.names_only else run_scripts(scripts, tts["overrides"], stt, voice, print)
    )
    name_rows = run_names(sentences, tts, stt, voice, print)
    metrics = summarize(script_rows, name_rows)
    params = {
        "tts": "kokoro-v1.0",
        "voice": voice,
        "stt": f"faster-whisper:{settings.stt_model}",
        "scripts_run": run_id,
        "n_scripts": 0 if args.names_only else len(scripts),
        "n_name_sentences": len(sentences),
    }
    report = report_markdown(metrics, name_rows, params)

    mlflow.set_experiment(EXPERIMENT)
    with mlflow.start_run(run_name=f"kokoro:{voice} + whisper:{settings.stt_model}") as run:
        mlflow.log_params(params)
        mlflow.log_metrics(metrics)
        mlflow.log_text(report, "report.md")
        mlflow.log_text(json.dumps(metrics, indent=2, sort_keys=True), "summary.json")
        mlflow.log_text(
            _csv(["reference", "transcript", "ref_words", "errors"],
                 [[r.reference, r.transcript, r.ref_words, r.errors] for r in script_rows]),
            "scripts_wer.csv",
        )  # fmt: skip
        mlflow.log_text(
            _csv(["condition", "kind", "reference", "transcript", "names_correct"],
                 [[r.condition, r.kind, r.reference, r.transcript,
                   " ".join(f"{h.name}={h.correct}" for h in r.hits)] for r in name_rows]),
            "names.csv",
        )  # fmt: skip
        # One listenable before/after sample.
        sample = "Your plan covers lisinopril and atorvastatin at the lowest copay tier."
        with tempfile.TemporaryDirectory() as tmp:
            for condition in CONDITIONS:
                path = Path(tmp) / f"{condition}.wav"
                path.write_bytes(tts[condition].synthesize(sample, voice=voice).to_wav())
                mlflow.log_artifact(str(path), "samples")
        run_id_out = run.info.run_id
    print(report)
    print(f"[eval-voice] MLflow run {run_id_out} in experiment {EXPERIMENT!r}")


if __name__ == "__main__":
    main()
