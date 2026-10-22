# CareGap Voice

A deployable voice outreach agent for health plan care gaps: it finds members with open
gaps (annual wellness visit, diabetic eye exam, statin adherence), generates a compliant
outreach script, renders it to speech, places the call, and answers inbound callback
questions from plan benefit documents.

> **All data is synthetic.** Members are generated with [Synthea](https://synthetichealth.github.io/synthea/).
> There is no real PHI anywhere in this repository, its fixtures, or its demo recordings.

**Status:** Phase 0 (scaffold). See [PROGRESS.md](PROGRESS.md).

## Design goals

- **Local first, near-zero cost.** The default stack runs entirely in docker compose:
  Ollama for the LLM, Kokoro for TTS, faster-whisper for STT, a local Twilio mock.
  Paid APIs are optional and sit behind a hard budget cap (`LLM_BUDGET_USD`, default $5).
- **Measured, not claimed.** Rule-based evals, LLM-as-a-judge calibrated against human
  labels, round-trip WER for voice, hallucination analysis for RAG.
- **Config-driven multi-tenancy.** Two customer configs drive every behavior difference.

## Quick start

Requires Docker with Compose v2, [uv](https://docs.astral.sh/uv/), and GNU make.

```bash
make install      # Python deps + pre-commit hooks
make up           # start Postgres+pgvector, MLflow, Ollama, HAPI FHIR; waits until healthy
make pull-model   # optional now, needed from Phase 3: pulls qwen2.5:3b (~2 GB)
make check        # lint, typecheck, unit tests
make test-all     # adds integration tests against the running stack
```

| Service    | URL                          |
|------------|------------------------------|
| Postgres   | `localhost:5432` (caregap/caregap) |
| MLflow     | http://localhost:5000        |
| Ollama     | http://localhost:11434       |
| HAPI FHIR  | http://localhost:8080/fhir   |

HAPI FHIR takes a few minutes to become healthy on first start.

## Architecture

```
Synthea -> HAPI FHIR -> bulk export (NDJSON)
        -> Databricks Free Edition (bronze/silver/gold, gap detection) -> local Postgres
Postgres (members, gaps, consent, outreach state, audit, cost ledger, pgvector)
Core service (FastAPI + MCP): script generation, compliance, TTS, RAG Q&A
n8n: orchestration only. MLflow: prompts, evals, traces.
```

Design decisions are recorded in [docs/adr](docs/adr).

## Results

_To be filled in as phases land._

## Limitations

_To be filled in._
