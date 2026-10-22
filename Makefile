# CareGap Voice. Run `make help` for targets.
SHELL := /bin/bash
.DEFAULT_GOAL := help
COMPOSE := docker compose
UV := uv run

# Load .env so targets see the same settings as the app and compose.
-include .env
export

.PHONY: help
help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(firstword $(MAKEFILE_LIST)) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- setup -------------------------------------------------------------------
.PHONY: install
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

# --- later phases (placeholders) ---------------------------------------------
define todo
	@echo "make $@: not implemented yet ($(1))"; exit 1
endef

.PHONY: eval eval-voice run-campaign dashboard
eval: ## [Phase 3/8] Run eval suite
	$(call todo,Phase 3)
eval-voice: ## [Phase 4] TTS round-trip WER eval
	$(call todo,Phase 4)
run-campaign: ## [Phase 5] End-to-end campaign run
	$(call todo,Phase 5)
dashboard: ## [Phase 10] Streamlit dashboard
	$(call todo,Phase 10)
