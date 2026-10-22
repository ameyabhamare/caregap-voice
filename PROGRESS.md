# Progress

## Phases

| # | Phase | Status |
|---|-------|--------|
| 0 | Scaffold | Approved |
| 1 | Synthetic data | Not started |
| 2 | Databricks pipeline + gap detection | Not started |
| 3 | Core service, generation, compliance | Not started |
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

## Decisions log
| Date | Decision | Why |
|------|----------|-----|
| 2026-10-22 | uv, Python 3.12 | Approved in Phase 0 plan. 3.12 has the widest support among later deps. |
| 2026-10-22 | Default local model `qwen2.5:3b` | Approved. Small, CPU-friendly, good at following instructions. |
| 2026-10-22 | MLflow tracking store in a separate `mlflow` DB on the same Postgres | One Postgres to run. Approved. |
| 2026-10-22 | HAPI FHIR on H2 (file, named volume) | Approved; revisit in Phase 1. |
| 2026-10-22 | Ledger reserves worst-case cost before a call, settles actual after | Concurrent calls can't overspend; see ADR 0002. |
| 2026-10-22 | Failed call with no usage report is charged at the estimate | The cap must overcount, never undercount. |
| 2026-10-22 | Local ($0) calls allowed even after the cap is hit | The default stack must keep working; they are still logged. |
| 2026-10-22 | HAPI default (distroless) image, using its bundled `HealthCheck` probe | The `-tomcat` variant cannot unpack its WAR as non-root and took more than 14 min without ever becoming healthy; the default image is healthy in about 35 s. |

## Open questions
- Claude prices in `src/caregap/cost/pricing.py` were last checked 2026-10-06. Re-check before Phase 11.
- Paid TTS pricing (OpenAI, ElevenLabs) is not in the pricing table yet; add it in Phase 4
  with the stubbed providers. Unpriced paid providers are refused until then.
- No CI workflow until Phase 8, per the brief.
