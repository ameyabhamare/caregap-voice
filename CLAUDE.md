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
  cost/               pricing table + cost ledger with budget cap
  db/schema.sql       idempotent app schema (also mounted as a Postgres init script)
  data/               Synthea run, statin fill simulator, edge-case builder, FHIR load,
                      bulk export, export validation (`python -m caregap.data <step>`)
  pipeline/           PySpark medallion pipeline + gap detection (local Parquet or UC Delta)
  gaps/sync.py        gold snapshot -> Postgres members/care_gaps
  llm/                LLM providers (Ollama, Claude) + LLMClient (metered by the ledger)
  outreach/           profile, compliance rules, script generator, Postgres store
  api/app.py          FastAPI core service (`make serve`)
  prompts.py          prompt files <-> MLflow prompt registry
  voice/              TTS (Kokoro), paid stubs, pronunciation overrides, STT, WER, rendering
  evals/              eval suites logged to MLflow (`make eval-rules`, `make eval-voice`)
voice/pronunciations.yaml  IPA + respelling overrides for drug and plan names
fixtures/voice/       name sentences for the pronunciation eval
prompts/              prompt source of truth (YAML); register with `make prompts-register`
fixtures/edge_cases/  hand-built edge-case members with expected gap outcomes
synthea/              Synthea properties (run parameters are pinned in caregap/data/synthea.py)
databricks.yml        Databricks Asset Bundle: serverless job running the same pipeline
tests/unit/           no services needed; ledger contract tests also run against Postgres
tests/integration/    need `make up`; marked `integration`
docker/               per-service build context and init files
docs/adr/             architecture decision records
```

## Commands
- `make up` / `make down` / `make ps` / `make logs`; `make nuke` deletes volumes.
- `make check`: ruff, mypy --strict, unit tests. Run before every commit.
- `make test-all`: unit + integration (fails rather than skips if Postgres is down).
- `make fmt` auto-fixes lint and formatting.
- `make seed` generates ~2,000 members + edge cases, loads HAPI, exports NDJSON to
  `data/export/`, validates and writes `manifest.json`. `SEED_POPULATION=20` for a quick run.
- `make seed-verify` seeds twice from an empty FHIR server and compares content digests.
- `make reset-fhir` empties the HAPI database.
- `make prompts-register` after editing `prompts/*.yaml`; `make serve` runs the API.
- `make eval-rules N=100` runs the rule-based compliance eval and logs to MLflow.
- `make eval-voice` runs the TTS -> STT round trip (WER, name WER before/after overrides).
- Tests marked `models` load real TTS/STT files from `data/cache`; `make check` skips them.
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
- Only `OutreachProfile.allowed_member_fields` may reach a prompt.
- New drug or plan names go in `voice/pronunciations.yaml` and `fixtures/voice/name_sentences.yaml`
  (a test keeps them in sync and checks IPA symbols against Kokoro's vocabulary).
- Edge-case expectations must follow from the definitions in the YAML header; the oracle
  test enforces it. Changing a definition means updating the header, the oracle and Phase 2.
