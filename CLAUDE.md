# CLAUDE.md

Conventions and context for working on CareGap Voice. Keep this short and current.

## Working agreement
- Build one phase at a time (see PROGRESS.md). Post a plan, wait for "go"; at the end
  post a phase report and wait for "approved" before starting the next phase.
- Small commits, one logical change each. Say explicitly if a change makes tests or evals worse.
- Never add a paid service, new dependency category, or new infrastructure component
  without asking first.
- Scope guard: smaller and fully tested beats more features. Ask instead of expanding.

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

## Conventions
- Python 3.12, uv, ruff (line length 100), mypy strict on `src` and `tests`.
- Money is `Decimal`, never float.
- Schema changes go in `src/caregap/db/schema.sql` and must stay idempotent
  (`IF NOT EXISTS`). No migration tool yet; revisit when a schema change needs one.
- Pin container images and in-container packages to exact versions.
- New ADR for each key tool choice: `docs/adr/NNNN-title.md`.
