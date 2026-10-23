# Progress

## Phases

| # | Phase | Status |
|---|-------|--------|
| 0 | Scaffold | Approved |
| 1 | Synthetic data | Approved |
| 2 | Databricks pipeline + gap detection | Approved |
| 3 | Core service, generation, compliance | Approved |
| 4 | Voice (TTS + round trip) | Approved |
| 5 | Orchestration (n8n + Twilio mock) | Approved |
| 6 | Multi-customer config | Approved |
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
- [x] Acceptance: `make eval-rules N=100` with qwen2.5:3b - first-attempt pass 28%, final 65%, review 35% (MLflow run 6faee595)
- [x] Phase report posted, approved (2026-10-22)

## Phase 4: Voice (TTS + round trip)
- [x] `voice` dependency group (kokoro-onnx 0.6.1, faster-whisper 1.2.1)
- [x] TTS interface, Kokoro provider (model pinned by sha256), paid stubs disabled
- [x] Pronunciation overrides: 22 drugs + 4 plan names, IPA checked against Kokoro vocab
- [x] STT (faster-whisper small.en), aligned WER with name attribution, 56 name sentences
- [x] `script_audio` table, `POST/GET /scripts/{id}/audio`
- [x] `make eval-voice` with unit tests (fakes)
- [x] Acceptance: `make eval-voice` - script WER 2.3% (100 scripts); medication name accuracy
      54.5% -> 88.6% with overrides (dictionary v2, MLflow run 1431b6ee); plan names 100% both
- [x] Dictionary v1 made names worse (54.5% -> 36.4%, run e5edd9b9): stress-mark convention bug, fixed
- [x] Phase report posted, approved (2026-10-22)

## Phase 5: Orchestration (n8n + Twilio mock)
- [x] Campaign policy in the outreach profile: call window, retries, first-N approval
- [x] Schema: consent, campaigns, campaign_members, call_attempts, telephony_events, escalations
- [x] Synthetic consent (90/5/5, deterministic), `make seed-consent`
- [x] Campaign service + API: selection with skip reasons, idempotent claims, approval gate,
      call window, retries, DTMF opt-out/escalation, stuck-work recovery, generation cap,
      notification outbox
- [x] Twilio wire format (signatures, TwiML) and a local Twilio mock service
- [x] Core service, Twilio mock and n8n 1.121.3 in compose
- [x] Four n8n workflows (tick, Twilio callbacks, notification delivery, error alerts),
      `make n8n-import` / `make n8n-export`; test keeps them free of business logic
- [x] `make run-campaign` (20 members) - see the phase report for the run
- [x] Phase report posted, approved (2026-10-23)

## Phase 6: Multi-customer config
- [x] Customer config schema (pydantic) with per-file and cross-file validation,
      `make validate-config`, validated at core startup
- [x] Sunrise config reproducing the Phase 3-5 profile exactly; DEFAULT_PROFILE removed
- [x] Campaigns, scripts, selection, call windows, DTMF keys, alerts, voices per customer
- [x] Platform capabilities: Spanish (prompts, compliance terms, TTS) and no-name greeting
- [x] Sunrise EN + ES (config); Horizon added as a config-only commit (fdd3770)
- [x] Horizon campaign end to end on unchanged code; per-customer rule evals
- [x] Voice eval unchanged on the Phase 5 scripts: WER 2.3%, names 54.5% -> 88.6% (de216841)
- [x] Phase report posted, approved (2026-10-23)

## Resume here (hand-off between sessions)
Status: Phases 0-6 approved. Next: post the Phase 7 plan and wait for "go".
New sessions: start from docs/HANDOVER.md. If a new session
starts, rebuild local state first (the container is ephemeral): start dockerd if needed,
`make up`, `make seed`, `make pipeline`, `make sync-gaps`, `make prompts-register`, and
get qwen2.5:3b into Ollama (here: fetch manifest + blobs from registry.ollama.ai on the
host and `docker cp` them into `/root/.ollama/models/`, since containers have no DNS).

Environment quirks seen in the cloud container:
- The Docker daemon is not running after a container restart: `nohup dockerd &`.
- pip inside `docker build` needs the proxy CA (`/root/.ccr/ca-bundle.crt`) for the MLflow
  image - build it once from a scratch copy with the CA added.
- Databricks: the token is injected by the environment's network secret; set
  `DATABRICKS_HOST=https://dbc-02701db1-ff8f.cloud.databricks.com` and any placeholder
  `DATABRICKS_TOKEN` in `.env`. Install the Databricks CLI v1.22.1 from GitHub releases.
- PySpark 4.0.4 builds from source on first `uv sync` (several minutes).
- Kokoro model files download from GitHub releases into data/cache/kokoro on first use.
  Containers have no DNS here: render once on the host (or copy the files) before the
  `core` container needs them.
- Docker Hub rate-limits (429). `mirror.gcr.io/library/...` and `mirror.gcr.io/n8nio/n8n`
  work: pull from the mirror and `docker tag` to the pinned name.
- Image builds need the proxy CA: `cp /root/.ccr/ca-bundle.crt docker/extra-ca.pem` and
  `BUILD_EXTRA_CA_CERT=./docker/extra-ca.pem` in `.env` (gitignored).
- The disk allowance is small: `docker builder prune` and `uv cache clean` free GBs; avoid
  rebuilding the core image from an empty cache when space is low.
- Start the stack through `make` (it sets HOST_UID/GID); plain `docker compose up` runs
  `core` as uid 1000, which cannot write a root-owned `data/`.

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
| 2026-10-22 | Claude provider added now (mock-tested only), default model claude-opus-5-5 | Approved defaults in Phase 3 plan; Opus is $4/$20 per MTok vs Sonnet's $2/$10, still under the $5 cap. |
| 2026-10-22 | PHI: only the first name reaches the model; no diagnosis or drug names in scripts | Approved in Phase 3 plan. |
| 2026-10-22 | Strip Synthea's name digits in silver | Found in the first eval trial ("Hello Mickey576"). Phase 2 code change; gap results unchanged. |
| 2026-10-22 | kokoro-onnx (no PyTorch) for TTS, faster-whisper small.en for STT | Approved in Phase 4 plan. ADR 0006. |
| 2026-10-22 | PyAV pinned <19 | PyAV 19 removed an argument faster-whisper 1.2.1 passes; every transcription crashed. |
| 2026-10-22 | Override IPA follows espeak's stress convention (mark before the vowel) | v1 used dictionary convention and made names worse; v2 fixed it (+34 points). Test enforces it. |
| 2026-10-23 | All campaign decisions in the core service; n8n only schedules, routes and calls it | Brief: no business logic in n8n. A test limits node types and URLs. ADR 0007. |
| 2026-10-23 | n8n places the Twilio call with a request built by the core service | Matches the Phase 11 shape (n8n hands Twilio the audio URL). |
| 2026-10-23 | Callback signatures checked against the URL the core service issued | n8n relays callbacks; the relay must not be able to change what was signed. |
| 2026-10-23 | Unknown consent means no call; synthetic consent 90/5/5 | Synthea has no consent records. Approved default. |
| 2026-10-23 | First-N holds reuse the review queue (`pending_approval`); failed scripts count toward N | Approved default (N=5). |
| 2026-10-23 | Notifications through an outbox delivered by n8n, Slack-compatible | Channel outages delay, never lose, alerts. Internal IDs only. |
| 2026-10-23 | n8n 1.121.3 with SQLite (deviation: plan said Postgres) | 1.122+ cannot activate CLI-imported workflows; SQLite avoids creating a DB on existing volumes. |
| 2026-10-23 | At most 2 scripts generated at once | Found in the first 20-member run: unbounded claims queued 32 requests on Ollama. |
| 2026-10-23 | `CAMPAIGN_CLOCK` demo clock | `make run-campaign` must work at any real time of day. |
| 2026-10-23 | One YAML per customer, pydantic-validated, checked at startup | Brief: second customer with zero code changes. ADR 0008. |
| 2026-10-23 | Membership by age: Sunrise 65+, Horizon under 65 | Stand-in for enrollment data; MA vs ACA. Approved default. |
| 2026-10-23 | Spanish for Sunrise, not Horizon | 20 Spanish-speaking Sunrise members with gaps vs 3 under 65. Approved default. |
| 2026-10-23 | Unsupported member languages get the customer's default language | Approved default (about 4% of members). |
| 2026-10-23 | Language prompts as separate prompt names; English prompts untouched | Keeps prompt v1 as the Phase 8 baseline. |
| 2026-10-23 | Drug/diagnosis terms checked in every language; accent-insensitive matching | An English drug name in a Spanish script is still a drug name. |
| 2026-10-23 | Spanish voice WER deferred | small.en is English-only; multilingual Whisper is ~480 MB and disk is tight. Approved default. |
| 2026-10-22 | HAPI default (distroless) image, using its bundled `HealthCheck` probe | The `-tomcat` variant cannot unpack its WAR as non-root and took more than 14 min without ever becoming healthy; the default image is healthy in about 35 s. |

## Open questions
- (Decided) Prompt v1's "two sentences" bug (it supplies three) stays as the Phase 8
  baseline; fixing it is the first measured prompt iteration in Phase 8.
- `members` rows are upserted, never deleted, by the sync: a member who disappears from
  gold keeps their registry row (their care gaps for the year are removed). Phase 5 now
  references members from campaigns, so deleting them would also need outreach history
  handling; keeping the upsert-only behaviour.
- Spanish wording, prompts and term lists were written at dev time and need review by a
  native speaker.
- No Spanish round-trip (WER) eval yet: needs multilingual Whisper.
- (Resolved) Edge-case expectations and measure definitions reviewed and accepted.
- Statin prevalence is high in Synthea (738 of 2,000 members, 37%). Fine for testing; not
  representative of a real plan.
- Claude prices in `src/caregap/cost/pricing.py` were last checked 2026-10-06. Re-check before Phase 11.
- Paid TTS pricing (OpenAI, ElevenLabs) is not in the pricing table yet; add it in Phase 4
  with the stubbed providers. Unpriced paid providers are refused until then.
- No CI workflow until Phase 8, per the brief.
