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
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

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

# --- later phases (placeholders) ---------------------------------------------
define todo
	@echo "make $@: not implemented yet ($(1))"; exit 1
endef

.PHONY: seed export-fhir pipeline sync-gaps eval eval-voice run-campaign dashboard
seed: ## [Phase 1] Generate Synthea members and load into HAPI FHIR
	$(call todo,Phase 1)
export-fhir: ## [Phase 1] FHIR bulk export to NDJSON
	$(call todo,Phase 1)
pipeline: ## [Phase 2] Run Databricks medallion pipeline
	$(call todo,Phase 2)
sync-gaps: ## [Phase 2] Sync gold gap tables into local Postgres
	$(call todo,Phase 2)
eval: ## [Phase 3/8] Run eval suite
	$(call todo,Phase 3)
eval-voice: ## [Phase 4] TTS round-trip WER eval
	$(call todo,Phase 4)
run-campaign: ## [Phase 5] End-to-end campaign run
	$(call todo,Phase 5)
dashboard: ## [Phase 10] Streamlit dashboard
	$(call todo,Phase 10)
