# CLAUDE.md

Conventions and context for working on CareGap Voice. Keep this short and current.

## Working agreement
- The owner's brief is in `docs/BRIEF.md`; new sessions start from `docs/HANDOVER.md`.
- Build one phase at a time (see PROGRESS.md). Post a plan, wait for "go"; at the end
  post a phase report and wait for "approved" before starting the next phase.
- Small commits, one logical change each. Say explicitly if a change makes tests or evals worse.
- Never add a paid service, new dependency category, or new infrastructure component
  without asking first.
- Scope guard: smaller and fully tested beats more features. Ask instead of expanding.
- Watch context size; at a natural boundary, update `docs/HANDOVER.md` so a new session
  can take over.

## Hard rules
- **Synthetic data only.** Never add real PHI. Generated data lives under `data/` (gitignored).
- **Every runtime model call goes through `caregap.cost.CostLedger.guard`.** Paid calls
  are refused once `LLM_BUDGET_USD` is reached. Unpriced paid models fail closed.
- Runtime LLM calls go through `LLM_PROVIDER` (`ollama` default, `claude` optional).
  Never use Claude Code itself as a runtime backend.
- CI never calls paid APIs or large local models; it uses recorded fixtures.
- n8n holds no business logic; it calls core service APIs only.

## Layout
```
src/caregap/          Python package
  config.py           pydantic-settings, env vars documented in .env.example
  customers.py        customer config schema, cross-checks, loader (`make validate-config`)
  cost/               pricing table + cost ledger with budget cap
  db/schema.sql       idempotent app schema (also mounted as a Postgres init script)
  data/               Synthea run, statin fill simulator, edge-case builder, FHIR load,
                      bulk export, export validation (`python -m caregap.data <step>`)
  pipeline/           PySpark medallion pipeline + gap detection (local Parquet or UC Delta)
  gaps/sync.py        gold snapshot -> Postgres members/care_gaps
  llm/                LLM providers (Ollama, Claude) + LLMClient (metered by the ledger)
  outreach/           per-language profile view, compliance rules (EN+ES), generator, store
  api/app.py          FastAPI core service (`make serve`, or the `core` compose service)
  campaign/           campaign state machine, consent, call window, clock, run-campaign driver
  telephony/          Twilio wire format (signatures, TwiML) and the local Twilio mock
  rag/                benefits documents (md -> PDF -> sections), pgvector chunks + retriever,
                      callback agent with grounding check, callback service
  members.py          what an agent may see/do for a member (MCP tools' rules)
  observability.py    MLflow tracing (one trace per core operation; off in tests/CI)
  audit.py            append-only audit log + a member's campaign timeline
  dashboard/          Streamlit dashboard (`make dashboard`); queries in data.py
  mcp/                MCP server over the core HTTP API (stdio; `python -m caregap.mcp`)
  prompts.py          prompt files <-> MLflow prompt registry
  voice/              TTS (Kokoro), paid stubs, pronunciation overrides, STT, WER, rendering
  evals/              eval suites logged to MLflow: rules, voice, rag, judge + calibration,
                      the script suite with promotion (`make eval`), the CI gate (`ci.py`)
voice/pronunciations.yaml  IPA + respelling overrides for drug and plan names
fixtures/voice/       name sentences for the pronunciation eval
prompts/              prompt source of truth (YAML); register with `make prompts-register`
customers/            one YAML per health plan (sunrise, horizon): all behavior differences
benefits/             synthetic Summary of Benefits per customer (markdown source + committed PDF)
fixtures/qa/          golden callback Q&A per customer
fixtures/judge/       judge calibration set (owner labels only; never pre-fill)
fixtures/ci/          recorded model/embedding responses, sampled members, thresholds (CI replay)
reports/              per-customer eval reports (markdown + JSON), written by record/CI
.github/workflows/    CI: lint, types, unit, Postgres integration, fixture eval gate
fixtures/edge_cases/  hand-built edge-case members with expected gap outcomes
synthea/              Synthea properties (run parameters are pinned in caregap/data/synthea.py)
databricks.yml        Databricks Asset Bundle: serverless job running the same pipeline
tests/unit/           no services needed; ledger contract tests also run against Postgres
tests/integration/    need `make up`; marked `integration`
docker/               per-service build context and init files (core image: docker/core)
n8n/workflows/        n8n workflow exports (orchestration only; `make n8n-import`)
docs/adr/             architecture decision records
```

## Commands
- `make up` / `make down` / `make ps` / `make logs`; `make nuke` deletes volumes.
- `make check`: ruff, mypy --strict, unit tests. Run before every commit, with `make lint`
  (it includes the format check CI runs).
- `make test-all`: unit + integration (fails rather than skips if Postgres is down).
- `make fmt` auto-fixes lint and formatting.
- `make seed` generates ~2,000 members + edge cases, loads HAPI, exports NDJSON to
  `data/export/`, validates and writes `manifest.json`. `SEED_POPULATION=20` for a quick run.
- `make seed-verify` seeds twice from an empty FHIR server and compares content digests.
- `make reset-fhir` empties the HAPI database.
- `make prompts-register` after editing `prompts/*.yaml`; `make serve` runs the API.
- `make eval-rules N=100 CUSTOMER=sunrise [LANGUAGE=es]` runs the rule-based compliance
  eval for one customer and logs to MLflow.
- `make validate-config` checks every customer config (the core service also does at startup).
- `make benefits-pdf` after editing `benefits/*.md` (a test fails if the PDFs are stale),
  then `make ingest-benefits`. `make eval-rag CUSTOMER=...` runs the callback Q&A eval.
- Prompt iteration: edit `prompts/*.yaml`, `make prompts-candidate`, `make eval ALIAS=candidate`
  (promotes on improvement; on reject, restore the file). `make eval` records a baseline.
- `make record-fixtures` after a promoted prompt or any change to how requests are built
  (about 2 hours, live models); review the new numbers, commit `fixtures/ci` and `reports/`.
  `make eval-ci` replays them (what CI runs). `make label-judge`, `make judge-agreement`.
- `make eval-voice` runs the TTS -> STT round trip (WER, name WER before/after overrides).
- Tests marked `models` load real TTS/STT files from `data/cache`; `make check` skips them.
- `make run-campaign CUSTOMER=horizon` imports the n8n workflows and runs a 20-member
  campaign end to end (`CAMPAIGN_SIZE=...`, default customer sunrise). `make n8n-export` after editing workflows in the n8n UI.
- `make mcp-config` prints the Claude Desktop entry for the MCP server (docs/mcp.md);
  `make mcp` runs it on stdio. `make db-schema` applies schema changes to an old volume.
- `make dashboard` (http://localhost:8501); `make trace-member CAMPAIGN=.. MEMBER=..` prints
  a member's audit timeline.
- `make pipeline` (local PySpark) then `make sync-gaps`. `make pipeline-databricks` runs the
  job on Databricks serverless and syncs its output instead.

## Conventions
- Python 3.12, uv, ruff (line length 100), mypy strict on `src` and `tests`.
- Money is `Decimal`, never float.
- Schema changes go in `src/caregap/db/schema.sql` and must stay idempotent
  (`IF NOT EXISTS`). No migration tool yet; revisit when a schema change needs one.
- Pin container images and in-container packages to exact versions.
- New ADR for each key tool choice: `docs/adr/NNNN-title.md`.
- Clinical codes live in `caregap/data/codes.py`; reuse them, don't re-type codes.
- Resources we create ourselves carry a `urn:caregap:synthetic` meta tag
  (`edge-case` or `simulated-fill`).
- Pipeline code uses the DataFrame API and Spark SQL only (no RDDs/SparkContext), so it
  runs on Databricks serverless. Local PySpark stays pinned to serverless environment 4.
- Application code calls models only through `LLMClient` (never a provider directly).
- Never send sampling parameters to Claude; tune with `CLAUDE_EFFORT`.
- Callback answers are spoken only after the grounding check in `caregap.rag.agent`; never
  bypass it. Embeddings go through `EmbeddingClient` (metered), like chat calls.
- Prompt changes: one change per eval run, promote only on improvement, roll back by
  moving the `production` alias (and restoring the file).
- Only `allowed_member_fields` may reach a prompt (validation checks each configured
  prompt's variables against them).
- Anything that differs between customers goes in `customers/*.yaml`, never in code. A new
  capability (language, measure) goes in the platform first, with tests, then a config
  turns it on.
- Prompt changes follow the promotion rule in `caregap.evals.suite`; never move the
  production alias by hand except to roll back.
- n8n workflows may only use trigger, HTTP, split, switch/if and respond nodes, and only
  call `$env.CORE_URL` or URLs the core service returns (a test enforces it). Any rule
  goes in `caregap.campaign.service`.
- Campaign state changes are conditional updates under row locks; keep endpoints
  idempotent, since n8n executions overlap.
- The MCP server is a thin layer like n8n: one core API call per tool, rules in
  `caregap.members`. Agent free text goes in `note`/`summary`, never in escalation
  `detail` (notifications carry IDs only).
- Governed changes write an `audit.record` event with the same connection, in the same
  transaction. Audit payloads hold IDs and outcomes, not free text. New core operations
  get a root `observability.span(..., root=True, tags=...)`; deeper code uses child spans.
- n8n is pinned to 1.121.3: later versions cannot activate CLI-imported workflows (ADR 0007).
- New drug or plan names go in `voice/pronunciations.yaml` and `fixtures/voice/name_sentences.yaml`
  (a test keeps them in sync and checks IPA symbols against Kokoro's vocabulary).
- Edge-case expectations must follow from the definitions in the YAML header; the oracle
  test enforces it. Changing a definition means updating the header, the oracle and Phase 2.
