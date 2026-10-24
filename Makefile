# CareGap Voice. Run `make help` for targets.
SHELL := /bin/bash
.DEFAULT_GOAL := help
COMPOSE := docker compose
UV := uv run

# Load .env so targets see the same settings as the app and compose.
-include .env
# Containers that write to ./data run as you.
HOST_UID ?= $(shell id -u)
HOST_GID ?= $(shell id -g)
export

.PHONY: help
help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(firstword $(MAKEFILE_LIST)) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- setup -------------------------------------------------------------------
.PHONY: install validate-config benefits-pdf ingest-benefits db-schema mcp mcp-config
install: ## Install Python deps and pre-commit hooks
	uv sync
	$(UV) pre-commit install

.env:
	cp .env.example .env

# --- stack -------------------------------------------------------------------
.PHONY: up down ps logs nuke pull-model
up: .env ## Start all containers and wait until healthy
	$(COMPOSE) up -d --build --wait --wait-timeout 600
	@$(COMPOSE) ps

down: ## Stop containers (keeps volumes)
	$(COMPOSE) down

ps: ## Show container status
	$(COMPOSE) ps

logs: ## Tail container logs
	$(COMPOSE) logs -f --tail=100

nuke: ## Stop containers AND delete all volumes (data loss)
	$(COMPOSE) down -v

pull-model: ## Pull the default Ollama model (~2 GB download)
	$(COMPOSE) exec ollama ollama pull $(OLLAMA_MODEL)

# --- quality -----------------------------------------------------------------
.PHONY: lint fmt typecheck test test-all check
lint: ## Ruff lint + format check
	$(UV) ruff check .
	$(UV) ruff format --check .

fmt: ## Auto-fix lint and formatting
	$(UV) ruff check --fix .
	$(UV) ruff format .

typecheck: ## mypy --strict
	$(UV) mypy

test: ## Unit tests (no services needed)
	$(UV) pytest

test-all: ## Unit + integration tests (needs `make up`)
	CAREGAP_REQUIRE_SERVICES=1 $(UV) pytest -m ""

check: lint typecheck test ## Everything CI runs

# --- synthetic data (Phase 1) ------------------------------------------------
SEED_POPULATION ?= 2000
PG_USER := $(or $(POSTGRES_USER),caregap)

.PHONY: seed seed-verify reset-fhir
seed: ## Generate Synthea members + edge cases, load into HAPI, bulk export, validate
	$(UV) python -m caregap.data seed --population $(SEED_POPULATION)

reset-fhir: ## Empty the HAPI FHIR database (data loss)
	$(COMPOSE) stop hapi-fhir
	$(COMPOSE) exec -T postgres psql -q -U $(PG_USER) -d postgres \
		-c "DROP DATABASE IF EXISTS hapi WITH (FORCE)" -c "CREATE DATABASE hapi"
	$(COMPOSE) up -d --wait hapi-fhir

seed-verify: ## Seed twice from an empty FHIR server and check the exports are identical
	$(MAKE) reset-fhir seed
	cp data/export/manifest.json data/manifest.run1.json
	$(MAKE) reset-fhir seed
	$(UV) python -m caregap.data.validate data/manifest.run1.json data/export/manifest.json

# --- pipeline and gap detection (Phase 2) -------------------------------------
MEASUREMENT_YEAR ?= 2025

.PHONY: pipeline sync-gaps
pipeline: ## Bronze/silver/gold + gap detection locally (PySpark, Parquet in data/lake)
	$(UV) python -m caregap.pipeline.job --layer all --source data/export \
		--lake data/lake --year $(MEASUREMENT_YEAR)

sync-gaps: ## Load the gold snapshot (members, care gaps) into Postgres
	$(UV) python -m caregap.gaps.sync data/lake/snapshot

# Databricks (needs the databricks CLI and DATABRICKS_HOST + auth; see README).
DATABRICKS_CATALOG ?= workspace
DATABRICKS_SCHEMA ?= caregap
DBX_VARS := --var catalog=$(DATABRICKS_CATALOG) --var schema=$(DATABRICKS_SCHEMA) \
	--var measurement_year=$(MEASUREMENT_YEAR)
DBX_VOLUME := dbfs:/Volumes/$(DATABRICKS_CATALOG)/$(DATABRICKS_SCHEMA)/raw

.PHONY: pipeline-databricks databricks-validate
databricks-validate: ## Validate the Databricks Asset Bundle
	databricks bundle validate $(DBX_VARS)

pipeline-databricks: ## Deploy bundle, upload NDJSON, run the job on serverless, sync gaps
	databricks bundle deploy $(DBX_VARS)
	databricks fs cp -r --overwrite data/export $(DBX_VOLUME)/export
	databricks bundle run gap_pipeline $(DBX_VARS)
	rm -rf data/lake/snapshot-databricks
	databricks fs cp -r $(DBX_VOLUME)/gold_snapshot data/lake/snapshot-databricks
	$(UV) python -m caregap.gaps.sync data/lake/snapshot-databricks

# --- core service, generation, compliance (Phase 3) ----------------------------
N ?= 100

.PHONY: eval record-fixtures eval-ci prompts-register prompts-candidate serve eval-rules eval-voice eval-rag label-judge judge-agreement
eval-voice: ## Round-trip TTS->STT WER eval incl. name WER before/after overrides (MLflow)
	$(UV) python -m caregap.evals.voice

ALIAS ?= production
eval: ## Script eval suite (rules + judge + reading level) for ALIAS prompts; promotes candidates on improvement
	$(UV) python -m caregap.evals.suite --alias $(ALIAS)

record-fixtures: ## Record every model response CI replays, thresholds and reports (live models)
	$(UV) python -m caregap.evals.ci record

eval-ci: ## CI eval gate: replay recorded fixtures, check thresholds, write reports/
	$(UV) python -m caregap.evals.ci check

label-judge: ## Label the judge calibration scripts (owner; resumable)
	$(UV) python -m caregap.evals.calibration label

judge-agreement: ## Judge vs owner labels: accuracy and Cohen's kappa per criterion (MLflow)
	$(UV) python -m caregap.evals.calibration agreement

eval-rag: ## Callback Q&A hallucination report for CUSTOMER (golden set in fixtures/qa), MLflow
	$(UV) python -m caregap.evals.rag --customer $(CUSTOMER)

eval-rules: ## Compliance eval on N members of CUSTOMER (default sunrise; LANGUAGE=es to filter)
	$(UV) python -m caregap.evals.rules -n $(N) --customer $(CUSTOMER) $(if $(LANGUAGE),--language $(LANGUAGE))

serve: ## Run the core service API on http://localhost:8000
	$(UV) uvicorn --factory caregap.api.app:create_app --host 127.0.0.1 --port 8000

db-schema: ## Apply the idempotent schema to DATABASE_URL (after a schema change on old volumes)
	$(UV) python -m caregap.db

mcp: ## Run the MCP server on stdio (Claude Desktop starts it itself; see docs/mcp.md)
	$(UV) python -m caregap.mcp

mcp-config: ## Print the Claude Desktop config entry for this checkout
	@$(UV) python -m caregap.mcp.desktop_config

benefits-pdf: ## Render benefits/*.md to the committed PDFs (dev time)
	$(UV) python -m caregap.rag.documents build

ingest-benefits: ## Embed each customer's Summary of Benefits into pgvector (idempotent)
	$(UV) python -m caregap.rag.store

validate-config: ## Validate every customer config in customers/
	$(UV) python -m caregap.customers

prompts-register: ## Register changed prompts in MLflow and point "production" at them
	$(UV) python -m caregap.prompts

prompts-candidate: ## Register changed prompts under the "candidate" alias (for make eval)
	$(UV) python -m caregap.prompts register --alias candidate

# --- orchestration (Phase 5) ---------------------------------------------------
N8N_WORKFLOWS := cgCampaignTick01 cgNotifyOutbox01 cgTwilioEvents01
CAMPAIGN_SIZE ?= 20
CUSTOMER ?= sunrise

.PHONY: seed-consent n8n-import n8n-export run-campaign
seed-consent: ## Give members synthetic voice consent (keeps existing rows)
	$(UV) python -m caregap.campaign.consent

n8n-import: ## Load n8n/workflows into n8n and activate them (restarts n8n)
	$(COMPOSE) exec -T n8n n8n import:workflow --separate --input=/workflows
	for id in $(N8N_WORKFLOWS); do \
		$(COMPOSE) exec -T n8n n8n update:workflow --id=$$id --active=true || exit 1; done
	$(COMPOSE) restart n8n
	$(COMPOSE) up -d --wait n8n

n8n-export: ## Export workflows from n8n back into n8n/workflows (after editing in the UI)
	$(COMPOSE) exec -T -u node n8n sh -c 'rm -rf /tmp/wf && mkdir /tmp/wf && \
		n8n export:workflow --all --separate --pretty --output=/tmp/wf'
	for pair in cgCampaignTick01:campaign-tick cgTwilioEvents01:twilio-events \
		cgNotifyOutbox01:notify-outbox cgErrorAlerts001:error-alerts; do \
		$(COMPOSE) exec -T n8n cat /tmp/wf/$${pair%%:*}.json > n8n/workflows/$${pair#*:}.json \
			|| exit 1; \
	done
	@# Drop install-specific fields (owner project, timestamps, counters) so exports diff cleanly.
	python3 -c 'import json, pathlib; drop = {"shared", "createdAt", "updatedAt", \
		"triggerCount", "versionCounter", "staticData", "isArchived"}; \
		[p.write_text(json.dumps({k: v for k, v in json.loads(p.read_text()).items() \
		if k not in drop} | {"active": False}, indent=2) + "\n") \
		for p in pathlib.Path("n8n/workflows").glob("*.json")]'

run-campaign: n8n-import ## Campaign for CAMPAIGN_SIZE members of CUSTOMER through n8n + Twilio mock
	$(UV) python -m caregap.campaign.run --customer $(CUSTOMER) --size $(CAMPAIGN_SIZE) \
		$(CAMPAIGN_ARGS)

# --- later phases (placeholders) ---------------------------------------------
define todo
	@echo "make $@: not implemented yet ($(1))"; exit 1
endef

.PHONY: dashboard
dashboard: ## [Phase 10] Streamlit dashboard
	$(call todo,Phase 10)
