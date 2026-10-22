# CareGap Voice: project brief (from the project owner)

Committed so every session works from the same brief. Phase status lives in PROGRESS.md.

## What this is
A deployable voice outreach agent for health plan care gaps. Portfolio project for an AI
Forward Deployed Engineer role. Optimize for production realism, deployability, and
measurable evals over feature count. Every key tool choice gets a short ADR explaining why
it exists. All data is synthetic (Synthea). No real PHI anywhere. State this in the README.

## How we work (follow strictly)
- Build ONE phase at a time. Before writing code for a phase, post a short plan: files you
  will create or change, approach, and anything you need from the owner. Wait for "go".
- At the end of each phase, STOP. Post a phase report:
  1. What was built (files, components)
  2. Exact commands to run it and verify it
  3. Test and eval results
  4. Decisions made and any deviations from this brief, with reasons
  5. Known issues, open questions, and anything that got worse
- Before posting a phase report, review your own diff and run the full test and eval suite.
- Only start the next phase after the owner replies "approved".
- Within a phase, one logical change at a time, small commits with clear messages. If a
  change makes tests or evals worse, say so explicitly.
- Maintain CLAUDE.md (conventions, architecture, how to run) and PROGRESS.md (phase
  checklist, status, decisions log, open questions).
- Never add a paid service, new dependency category, or new infrastructure component
  without asking first.
- Scope guard: a smaller system that is fully tested, evaluated, and documented beats more
  features. If something is turning into a rabbit hole, stop and ask.
- When the conversation context gets large, write a hand-over (docs/HANDOVER.md) at a
  natural boundary so a new session can take over and conserve tokens.

## Cost policy (near-zero cost)
- Default mode runs fully local in docker compose with zero spend.
- Dev-time assets (code, tests, fixtures, synthetic documents, golden Q&A sets, edge cases)
  are written by Claude Code and committed, not generated at runtime via API calls.
- Runtime LLM calls go through LLM_PROVIDER: Ollama small local model is the default for all
  development. Claude API is optional (LLM_PROVIDER=claude), only for final eval runs and
  demo recordings, under a hard budget cap (default $5) tracked in a local cost ledger;
  refuse calls once the cap is hit. Never use Claude Code itself as a runtime backend.
- TTS: Kokoro (local) default and only required provider. Paid providers (OpenAI,
  ElevenLabs) behind the same interface, disabled by default, only for an optional quality
  comparison.
- STT: faster-whisper (local).
- Telephony: a local Twilio mock service is the default. Real Twilio trial (verified numbers
  only) only in the final demo phase.
- Databricks: Free Edition only. Vector store: pgvector in local Postgres.
- MLflow: local container (tracking, prompt registry, tracing).
- Public endpoints (demo phase only): Cloudflare Tunnel (free) and Cloudflare R2 free tier.
- CI (GitHub Actions, public repo): never calls paid APIs or large local models. Recorded
  LLM/TTS fixtures for evals in CI. Live evals run locally via make.

## Architecture
- Synthea -> local HAPI FHIR -> FHIR bulk export (NDJSON)
- Databricks Free Edition: medallion pipeline (bronze/silver/gold Delta) with PySpark + SQL
  gap detection. Gold tables synced to local Postgres by a script.
- Local Postgres: members, gaps, consent, outreach state, interaction log, audit log, cost
  ledger, pgvector embeddings.
- Core service (Python, FastAPI): script generation, compliance checking, TTS routing, RAG
  Q&A. Also exposed as an MCP server.
- n8n (self-hosted): orchestration only. No business logic in n8n.
- MLflow (local): prompt registry, eval runs, tracing.
- Two customer configs drive all behavior differences.

## Phases (0-4 done; see PROGRESS.md)
0. Scaffold. 1. Synthetic data. 2. Databricks pipeline + gap detection.
3. Core service, generation, compliance. 4. Voice (TTS + round trip).

### Phase 5: Orchestration (n8n + Twilio mock)
- Local Twilio mock service (outbound message, inbound callback, DTMF events)
- n8n workflows: scheduled campaign respecting call windows and consent, human approval for
  first N messages, delivery via mock, retries, failure alerts, escalation queue (Slack free
  webhook or local log)
- n8n calls core service APIs only; workflows exported as JSON into repo
Acceptance: end-to-end campaign run for 20 members via `make run-campaign`.

### Phase 6: Multi-customer config
- Two YAML configs: "Sunrise Medicare Advantage" and "Horizon ACA Marketplace", differing in
  gap measures, disclaimers, languages (EN + ES for one), voice, call windows, escalation
  rules, allowed PHI fields
- Config schema validation (pydantic)
Acceptance: second customer runs end to end with zero code changes. Prove it in the phase
report (show the diff is config only).

### Phase 7: RAG callback agent + hallucination analysis
- Synthetic Summary of Benefits PDFs per customer (written at dev time), chunked into pgvector
- LangChain RAG for inbound callback Q&A; refuses out-of-scope and medical advice questions,
  escalates
- Golden Q&A set per customer (drafted at dev time, owner reviews); faithfulness scoring;
  failure categories (fabricated benefit, wrong cost-share, wrong plan, missed refusal)
Acceptance: hallucination report per customer logged to MLflow.

### Phase 8: LLM-as-a-Judge, prompt iteration, CI gate
- Judge rubric: clarity, tone, reading level, no medical advice
- Calibrate judge against ~50 examples labeled by the owner. Provide a labeling CSV and a
  simple labeling script. Do not pre-fill or suggest labels. Report agreement.
- Prompt iteration: each version is an MLflow run against the full eval suite; one change
  per version; promote only on improvement; flag regressions
- Record fixtures for CI at dev time; GitHub Actions runs lint, tests, and fixture-based eval
  gate with thresholds
- Per-customer eval report (markdown + JSON)
Acceptance: CI green on fixtures; prompt version history visible in MLflow.

### Phase 9: MCP server
- Expose core as MCP server: get_member_gaps, search_benefits, log_interaction,
  escalate_to_human
- Instructions and config for connecting Claude Desktop
Acceptance: Claude Desktop can inspect a member and trigger an escalation.

### Phase 10: Observability + governance
- MLflow tracing across generation, compliance, TTS, RAG
- Cost per message, latency p50/p95 per stage, WER tracked
- Audit log for every generated message: prompt version, model, eval scores, approver
- Small Streamlit dashboard: campaign funnel, eval pass rates, cost, latency
Acceptance: one campaign run fully traceable from member to audio to outcome.

### Phase 11: Live demo mode (optional spend, ask before enabling)
- Cloudflare Tunnel for n8n webhooks
- n8n uploads rendered audio to Cloudflare R2, passes Twilio a short-lived signed URL;
  lifecycle rule deletes audio after 24h
- Real Twilio trial to verified numbers only
- Final eval run with LLM_PROVIDER=claude within budget cap, compared against local results
- Optional: one-time paid TTS quality comparison vs Kokoro within budget cap
Acceptance: real call to the owner's verified number, recorded for the README.

### Phase 12: Docs and handoff
- Architecture diagram
- ADRs: n8n for orchestration only, MCP as integration surface, Databricks for batch data,
  local-first cost design, TTS strategy, PHI handling and the Twilio/R2 trust boundary
- Customer-facing RUNBOOK: deploy, add a customer, rotate keys, handle failures, roll back a
  prompt version
- README with results, eval numbers, local vs Claude comparison, demo recording, honest
  limitations
Acceptance: a fresh clone runs `make up && make seed && make run-campaign` following only
the README.
