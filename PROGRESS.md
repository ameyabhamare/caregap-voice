# Progress

## Phases

| # | Phase | Status |
|---|-------|--------|
| 0 | Scaffold | Approved |
| 1 | Synthetic data | Approved |
| 2 | Databricks pipeline + gap detection | Approved |
| 3 | Core service, generation, compliance | Built; acceptance eval blocked |
| 4 | Voice (TTS + round trip) | Not started |
| 5 | Orchestration (n8n + Twilio mock) | Not started |
| 6 | Multi-customer config | Not started |
| 7 | RAG callback agent + hallucination analysis | Not started |
| 8 | LLM-as-a-Judge, prompt iteration, CI gate | Not started |
| 9 | MCP server | Not started |
| 10 | Observability + governance | Not started |
| 11 | Live demo mode (optional spend) | Not started |
| 12 | Docs and handoff | Not started |

## Phase 0: Scaffold
- [x] Repo structure, CLAUDE.md, PROGRESS.md, README skeleton, .env.example
- [x] docker compose: Postgres+pgvector, MLflow, Ollama, HAPI FHIR, all with healthchecks
- [x] Makefile (real targets + placeholders for later phases), pre-commit (ruff, mypy), pytest
- [x] Cost ledger + budget cap enforcement, with tests
- [x] Approved (2026-10-22)

## Phase 1: Synthetic data
- [x] HAPI FHIR moved from H2 to Postgres (`hapi` database)
- [x] Synthea 4.0.0 (sha256-pinned), ~2,000 members aged 18-90, fixed seeds and dates
- [x] Simulated statin fills (Synthea has no dispenses or days supply)
- [x] 23 edge-case members with expected outcomes (draft, awaiting review)
- [x] Loader (stable IDs, idempotent), bulk export to NDJSON, validator + manifest
- [x] `make seed`, `make seed-verify`, `make reset-fhir`
- [x] Full-size `seed-verify` run: two runs from empty, identical content digest
- [x] Edge cases reviewed (definitions accepted as written)
- [x] Approved (2026-10-22)

## Phase 2: Databricks pipeline + gap detection
- [x] PySpark 4.0.4 pipeline group (matches Databricks serverless environment 4)
- [x] Bronze (raw NDJSON), silver (typed tables), gold (`member_gaps`, `members`)
- [x] Gap detection: AWV and eye exam in Spark SQL, statin PDC with window functions
- [x] Edge-case acceptance: 23 members x 3 measures match; targeted silver/gold tests
- [x] `members` / `care_gaps` tables and idempotent `make sync-gaps`
- [x] Databricks Asset Bundle; real run on Free Edition; output identical to local run
- [x] Approved (2026-10-22)

## Phase 3: Core service, generation, compliance
- [x] LLM providers (Ollama default, Claude via anthropic 1.x) behind metered `LLMClient`
- [x] Prompts in `prompts/`, registered in MLflow (`production` alias)
- [x] Rule-based compliance checker (8 rules) and outreach profile (built-in default)
- [x] Generator: generate -> check -> revise once -> `needs_review`
- [x] `outreach_scripts` table, FastAPI service (`make serve`), review queue endpoints
- [x] `make eval-rules` (MLflow logging) with unit tests
- [ ] **Acceptance: `make eval-rules N=100` with qwen2.5:3b** - blocked, see "Resume here"
- [ ] Phase report posted, approved

## Resume here (hand-off between sessions)
Status: Phase 3 is built, tested (237 tests) and pushed. Only the acceptance eval is left,
then the Phase 3 report. Decisions approved for Phase 3: defaults in the plan (Claude
provider now, cautious PHI allowlist: first name only, no diagnosis or drug names; one
script per member covering at most 2 gaps; built-in Sunrise default profile until Phase 6;
service runs on the host until Phase 5). Default Claude model changed to claude-opus-5-5.

Blocker: the cloud environment's network policy blocks the model download. The user was
asked to allow `registry.ollama.ai` and `*.r2.cloudflarestorage.com`.

Next steps:
1. Rebuild state (the container is ephemeral): start dockerd if needed, `make up`,
   `make seed` (~18 min), `make pipeline`, `make sync-gaps`, `make prompts-register`.
2. Get qwen2.5:3b into Ollama. Containers here cannot resolve outside DNS, so fetch the
   manifest and blobs from registry.ollama.ai on the host and copy them into the ollama
   volume (`/root/.ollama/models/{manifests,blobs}`); on a normal machine `make pull-model`.
3. `make eval-rules N=100`, then post the Phase 3 report (template in CLAUDE.md working
   agreement) with the MLflow metrics, and wait for "approved".

Environment quirks seen in the cloud container:
- The Docker daemon is not running after a container restart: `nohup dockerd &`.
- Docker Hub may rate-limit pulls; the stack's images were pulled fine before.
- Image builds need no proxy build args; pip inside `docker build` needs the proxy CA
  (`/root/.ccr/ca-bundle.crt`) for the MLflow image - build it once from a scratch copy.
- Databricks: the token is injected by the environment's network secret; set
  `DATABRICKS_HOST=https://dbc-02701db1-ff8f.cloud.databricks.com` and any placeholder
  `DATABRICKS_TOKEN` in `.env`. Install the Databricks CLI v1.22.1 from GitHub releases.
- PySpark 4.0.4 builds from source on first `uv sync` (several minutes).

## Decisions log
| Date | Decision | Why |
|------|----------|-----|
| 2026-10-22 | uv, Python 3.12 | Approved in Phase 0 plan. 3.12 has the widest support among later deps. |
| 2026-10-22 | Default local model `qwen2.5:3b` | Approved. Small, CPU-friendly, good at following instructions. |
| 2026-10-22 | MLflow tracking store in a separate `mlflow` DB on the same Postgres | One Postgres to run. Approved. |
| 2026-10-22 | HAPI FHIR on H2 (file, named volume) | Superseded in Phase 1: moved to Postgres. |
| 2026-10-22 | HAPI FHIR persists to a `hapi` database on the shared Postgres | ~700k resources and bulk export are beyond what H2 handles well. Approved in Phase 1 plan. |
| 2026-10-22 | Ledger reserves worst-case cost before a call, settles actual after | Concurrent calls can't overspend; see ADR 0002. |
| 2026-10-22 | Failed call with no usage report is charged at the estimate | The cap must overcount, never undercount. |
| 2026-10-22 | Local ($0) calls allowed even after the cap is hit | The default stack must keep working; they are still logged. |
| 2026-10-22 | Synthea runs on the HAPI image's Java 21 runtime | No extra image to download. Docker Hub rate-limited the JRE image pull here; also lighter for a fresh clone. ADR 0003. |
| 2026-10-22 | Synthea `-e 20260101` in addition to `-r` | Without an end date Synthea simulates to today, so output drifts and dates run past the as-of date. |
| 2026-10-22 | Export only gap-relevant resource types, 2 years of history | Removes EOB/Claim/DiagnosticReport/DocumentReference/Provenance; ~250 resources per member instead of ~550. |
| 2026-10-22 | Simulated statin `MedicationDispense` fills (60/25/15% adherent/partial/poor) | Synthea models statins as yearly renewals with no fills or days supply; adherence is otherwise unmeasurable. Deviation from the brief: our own data added to Synthea members, tagged `simulated-fill`. |
| 2026-10-22 | Edge cases as reviewable YAML + builder, checked by a reference oracle in tests | Reviewers check definitions and events, not FHIR JSON. Phase 2 must match every expectation. |
| 2026-10-22 | Loader rewrites creates to `PUT Type/<id>` | Stable IDs, idempotent re-runs, reproducible exports. |
| 2026-10-22 | Reproducibility = order- and server-metadata-independent content digest | HAPI adds `versionId`/`lastUpdated` and bulk export line order varies. |
| 2026-10-22 | Pipeline runs locally (default) and on Databricks from one codebase | Fresh-clone acceptance and CI can't require a Databricks account. Approved in Phase 2 plan. ADR 0004. |
| 2026-10-22 | Serverless environment version 4 + local pyspark 4.0.4 | Same Python (3.12) and Spark (4.0) in both places. |
| 2026-10-22 | Bundle runs the project wheel via `python_wheel_task` | Standard packaging; pyspark is provided by serverless, not a wheel dependency. |
| 2026-10-22 | Gold-to-Postgres via JSON-lines snapshot | No Spark, Arrow or SQL-warehouse client in the sync path; gold is ~6k rows. |
| 2026-10-22 | Sync replaces a whole measurement year in one transaction | Idempotent and readers never see a partial year. |
| 2026-10-22 | `members` holds name, phone and language (synthetic) | Needed for outreach. Approved in Phase 2 plan; PHI allowlist for prompts comes in Phase 3. |
| 2026-10-22 | HAPI default (distroless) image, using its bundled `HealthCheck` probe | The `-tomcat` variant cannot unpack its WAR as non-root and took more than 14 min without ever becoming healthy; the default image is healthy in about 35 s. |

## Open questions
- `members` rows are upserted, never deleted, by the sync: a member who disappears from
  gold keeps their registry row (their care gaps for the year are removed). Revisit with
  outreach history in Phase 5.
- (Resolved) Edge-case expectations and measure definitions reviewed and accepted.
- Statin prevalence is high in Synthea (738 of 2,000 members, 37%). Fine for testing; not
  representative of a real plan.
- Claude prices in `src/caregap/cost/pricing.py` were last checked 2026-10-06. Re-check before Phase 11.
- Paid TTS pricing (OpenAI, ElevenLabs) is not in the pricing table yet; add it in Phase 4
  with the stubbed providers. Unpriced paid providers are refused until then.
- No CI workflow until Phase 8, per the brief.
