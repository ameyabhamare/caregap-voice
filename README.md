# CareGap Voice

A deployable voice outreach agent for health plan care gaps: it finds members with open
gaps (annual wellness visit, diabetic eye exam, statin adherence), generates a compliant
outreach script, renders it to speech, places the call, and answers inbound callback
questions from plan benefit documents.

> **All data is synthetic.** Members are generated with [Synthea](https://synthetichealth.github.io/synthea/).
> There is no real PHI anywhere in this repository, its fixtures, or its demo recordings.

**Status:** Phase 2 (pipeline and gap detection). See [PROGRESS.md](PROGRESS.md).

## Design goals

- **Local first, near-zero cost.** The default stack runs entirely in docker compose:
  Ollama for the LLM, Kokoro for TTS, faster-whisper for STT, a local Twilio mock.
  Paid APIs are optional and sit behind a hard budget cap (`LLM_BUDGET_USD`, default $5).
- **Measured, not claimed.** Rule-based evals, LLM-as-a-judge calibrated against human
  labels, round-trip WER for voice, hallucination analysis for RAG.
- **Config-driven multi-tenancy.** Two customer configs drive every behavior difference.

## Quick start

Requires Docker with Compose v2, [uv](https://docs.astral.sh/uv/), GNU make, and Java 17+
(for the PySpark pipeline and its tests).

```bash
make install      # Python deps + pre-commit hooks
make up           # start Postgres+pgvector, MLflow, Ollama, HAPI FHIR; waits until healthy
make seed         # ~2,000 synthetic members + 23 edge cases -> HAPI FHIR -> NDJSON export
make pipeline     # bronze/silver/gold + care-gap detection with local PySpark
make sync-gaps    # load members and care gaps into Postgres
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

HAPI FHIR takes about a minute to become healthy on first start.

### Synthetic members

`make seed` generates members with a pinned Synthea release and fixed seeds, adds
simulated statin pharmacy fills (Synthea has none) and 23 hand-built edge-case members
with known expected gaps ([fixtures/edge_cases/members.yaml](fixtures/edge_cases/members.yaml)),
loads them into HAPI FHIR, bulk-exports NDJSON to `data/export/` and validates the export.
`make seed-verify` proves the run is reproducible. Use `SEED_POPULATION=20 make seed` for
a quick run. See [ADR 0003](docs/adr/0003-synthetic-data-pipeline.md).

### Gap detection

`make pipeline` runs the medallion pipeline locally (Parquet under `data/lake/`) and
detects three care gaps for measurement year 2025: annual wellness visit, diabetic eye
exam and statin adherence (PDC). `make sync-gaps` loads members and gaps into Postgres
(`members`, `care_gaps`). The first `uv sync` builds PySpark from source, which takes a
few minutes.

The same code runs on **Databricks Free Edition** as a serverless job writing Delta
tables in Unity Catalog ([ADR 0004](docs/adr/0004-gap-detection-pipeline.md)):

1. Install the [Databricks CLI](https://docs.databricks.com/dev-tools/cli/install.html).
2. Set `DATABRICKS_HOST` and `DATABRICKS_TOKEN` in `.env` (or use a CLI profile).
3. `make pipeline-databricks`: deploys the bundle, uploads the NDJSON to a volume, runs
   the job, downloads the gold snapshot and syncs it into Postgres.

> Upgrading from Phase 0: HAPI moved from H2 to Postgres. Run `make nuke && make up` once
> so Postgres creates the `hapi` database.

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
