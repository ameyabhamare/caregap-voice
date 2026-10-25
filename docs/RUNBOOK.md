# Runbook

How to run CareGap Voice for a health plan: deploy it, add a customer, rotate keys,
handle failures and roll back a prompt. Every member is synthetic (see
[ADR 0014](adr/0014-phi-handling.md) for what real data would need).

Commands run from the repository root. `make help` lists every target.

## 1. Deploy

### Local stack (default, $0)
Requirements: Docker with Compose v2, [uv](https://docs.astral.sh/uv/), GNU make,
Java 17+.

```bash
make install            # Python dependencies
make up                 # all containers; creates .env from .env.example on first run
make seed               # ~2,000 synthetic members (about 20 min)
make pipeline           # care-gap detection (local PySpark)
make sync-gaps          # members and gaps into Postgres
make pull-model         # qwen2.5:3b + nomic-embed-text into Ollama (~2.3 GB)
make prompts-register   # prompts/*.yaml -> MLflow, alias "production"
make ingest-benefits    # each plan's Summary of Benefits -> pgvector
make run-campaign       # 20-member campaign: n8n -> core service -> Twilio mock
```

Check it: `make ps` (all services healthy), http://localhost:8000/health, and
`make dashboard` (http://localhost:8501).

Every port binds to `127.0.0.1`. Nothing has authentication, so do not expose these
services to a network.

### Live mode (optional, real calls)
Follow [live-demo.md](live-demo.md). In short: Twilio trial, R2 bucket with a 1-day
lifecycle rule, `LIVE_*`/`TWILIO_*`/`R2_*` in `.env`, then `make live-up`,
`make live-call`, `make live-down`. The core service refuses to start if any live
setting is missing; `docker compose logs core` names it.

### After pulling new code
- `make up` rebuilds the core image (it bakes in `src/`, `customers/`, `prompts/`,
  `benefits/` and `voice/`).
- `make db-schema` applies schema changes to an existing database. The schema is
  idempotent, so running it again is safe.
- `make prompts-register` if `prompts/*.yaml` changed. `make ingest-benefits` if
  `benefits/` changed.

## 2. Add a customer
A new health plan needs no code changes for campaigns and callbacks. Everything goes in
config files ([ADR 0008](adr/0008-customer-configs.md)).

1. **Config.** Copy `customers/horizon.yaml` to `customers/<id>.yaml` and edit it. The
   fields are:
   - plan name, member age range and the measures to run;
   - `allowed_member_fields`: what may reach a prompt (keep it minimal);
   - per language: voice, prompts, the verbatim disclaimer and opt-out, and the callback
     phrases (the greeting and `qa_more` must name the callback digit);
   - call window, retries, the first-N approval count;
   - opt-out and callback digits.

   The schema and its cross-checks are in `src/caregap/customers.py`.
2. **Benefits document.** Write `benefits/<id>.md` (same structure as the existing
   ones), then run `make benefits-pdf`. Commit both files: a test fails if the PDF is
   stale.
3. **Golden questions.** Add `fixtures/qa/<id>.yaml` with answerable questions (and the
   expected amounts) plus questions that must be handed off: medical, member-specific,
   off-topic and not in the document.
4. **Pronunciations.** Add any new drug or plan names to `voice/pronunciations.yaml` and
   `fixtures/voice/name_sentences.yaml`.
5. **Validate and load:**
   ```bash
   make validate-config
   make up                      # rebuild the core image with the new files
   make ingest-benefits
   make eval-rules N=50 CUSTOMER=<id>
   make eval-rag CUSTOMER=<id>
   make run-campaign CUSTOMER=<id> CAMPAIGN_SIZE=5
   ```
6. **CI.** To include the customer in the script eval suite and the CI gate, add a line
   to `CONFIGS` in `src/caregap/evals/suite.py`. Then run `make record-fixtures` (about
   2 hours, local models) and commit `fixtures/ci/` and `reports/`.

A new language or measure is a platform change first (code and tests). After that,
config turns it on for a customer.

## 3. Rotate keys
Secrets live only in `.env` (gitignored) or the deployment's secret store. After
changing `.env`, restart what reads it: `docker compose up -d core` for the core
service. Commands run through `make` pick up `.env` themselves.

| Secret | Where to rotate | Then |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic Console -> API Keys: create, update `.env`, delete the old key | `docker compose up -d core` |
| `TWILIO_AUTH_TOKEN` | Twilio Console -> Account -> API keys & tokens: promote the secondary token | update `.env`, `docker compose up -d core`, then `make n8n-import` (n8n's copy of the credential) |
| `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` | Cloudflare -> R2 -> Manage API tokens: new token for the one bucket, delete the old one | update `.env`, `docker compose up -d core` |
| `DATABRICKS_TOKEN` | Databricks -> Settings -> Developer -> Access tokens | update `.env` (or your CLI profile) |
| Postgres password | change it in Postgres and in `POSTGRES_PASSWORD` / `DATABASE_URL` | `make down && make up` |

**n8n keeps its own copy of the Twilio credential** (encrypted, "CareGap: Twilio
account"), loaded from `.env` by `make n8n-import`; `make run-campaign` and `make live-up`
run it too. Keep it in step with the core service after any Twilio change, or calls fail
with 401 ([ADR 0007](adr/0007-orchestration-n8n-and-twilio-mock.md)).

**Upgrading from a version before this change:** older n8n executions hold the auth token
they were sent. Delete them once in the n8n UI (http://localhost:5678 -> Executions ->
select all -> Delete; the first visit asks you to create a local owner account), then
rotate the Twilio token if those executions were from live calls.

Never paste a key into a chat, an issue or a commit. If one leaks, delete it at the
provider first, then rotate.

## 4. Handle failures
Start with the dashboard (`make dashboard`) and `docker compose logs <service>`. To
follow one member: `make trace-member CAMPAIGN=<id> MEMBER=<member_id>`.

| Symptom | Likely cause | What to do |
|---|---|---|
| Scripts waiting in the review queue | the first N of a campaign, or a script failed compliance twice | `GET /reviews`; approve or reject with `POST /reviews/{id}` `{"decision": "approve", "reviewer": "<name>"}` |
| A call failed 3 times (Horizon: 2) | no answer, busy, or a bad number | member ends `failed`, with a `delivery_failed` escalation; follow up by hand |
| Escalations not arriving | notification webhook down | they stay in the outbox (`GET /notifications/outbox`) and n8n retries every minute; fix `NOTIFY_WEBHOOK_URL` |
| Member stuck in `preparing` or `calling` | crashed worker or lost Twilio callback | nothing to do: `/work` recovers it after 15 min (preparing) or 30 min (calling) |
| Nothing happens in a campaign | outside the call window, or n8n workflows not active | check `CAMPAIGN_CLOCK` and the customer's window; `make n8n-import` reloads and activates the workflows |
| API returns 402 | paid spend reached `LLM_BUDGET_USD` | intended. Switch to `LLM_PROVIDER=ollama`, or raise the cap deliberately |
| API returns 409 | invalid state change (already approved, already claimed) | usually an overlapping retry; check the member's state before acting |
| Core service won't start | a customer config fails validation, or live mode is half-configured | `docker compose logs core`; `make validate-config` |
| Callback answers are all hand-offs | benefits not ingested, or embedding model missing | `make pull-model`, `make ingest-benefits` |
| Slow scripts or answers (over 30 s) | Ollama on CPU, or two campaigns at once | expected on CPU (~10 s each); one campaign at a time |
| Every call fails to dispatch (HTTP 401 from Twilio or the mock) | n8n's Twilio credential differs from `.env` | `make n8n-import` |
| Live call: Twilio says "application error" | stale tunnel URL (a new `make live-up`), so signatures fail | rerun `make live-up` and update the Twilio number's webhook |
| `make seed` fails partway | HAPI not healthy yet, or disk full | `make ps`; free disk; `make reset-fhir` and rerun |

A member who pressed the opt-out digit has their consent revoked and is never called
again. Never re-grant consent by hand without a record of the member asking for it.

## 5. Roll back a prompt version
Prompts are versioned in MLflow. The service loads each prompt by its `production`
alias, so a rollback needs no deploy.

1. Find the last good version. In MLflow (http://localhost:5000 -> Prompts ->
   `<prompt name>`), each version lists its text. The `eval-suite` experiment shows which
   run promoted which version (tag `promoted`).
2. Move the alias back:
   ```bash
   export NAME=outreach-script VERSION=3   # the prompt and its last good version
   uv run python -c "import mlflow, os; mlflow.set_tracking_uri('http://localhost:5000'); \
     mlflow.genai.set_prompt_alias(os.environ['NAME'], 'production', int(os.environ['VERSION']))"
   ```
   (or set the alias on that version in the MLflow UI).
3. Restore `prompts/<name>.yaml` to that version's text (`git log -p prompts/<name>.yaml`),
   so the repository matches production. `make prompts-register` then reports no change.
4. Run `make eval` to record the rolled-back version as the new baseline. If any
   request-building code changed, run `make record-fixtures` too.

Rolling back is the only time the alias is moved by hand. Forward changes go through
`make prompts-candidate` and `make eval ALIAS=candidate`, which promotes only on
improvement ([ADR 0010](adr/0010-evals-judge-and-ci-gate.md)).
