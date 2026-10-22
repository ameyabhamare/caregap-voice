# 2. Local-first cost design and the budget cap

Date: 2026-10-22. Status: Accepted.

## Context
The system must run at zero cost by default, while still allowing a small, bounded
amount of paid spend (Claude API, optional paid TTS) for final evals and the demo.
A runaway eval loop or a bug must not be able to spend more than the cap.

## Decision
- Every runtime dependency has a free local default: Ollama (LLM), Kokoro (TTS),
  faster-whisper (STT), a Twilio mock, pgvector, MLflow in a container.
- Paid providers sit behind the same interfaces and are disabled by default.
- All model calls go through `CostLedger.guard`, backed by the `cost_ledger` table:
  1. **Reserve** the worst-case cost (prompt tokens plus `max_output_tokens`) before the call.
     The check and insert are serialized with a Postgres advisory lock, so concurrent
     workers cannot both pass the check and overspend together.
  2. **Settle** with the provider-reported usage after the call.
  3. If the call fails without usage, **charge the estimate**. We cannot tell whether
     the provider billed it, and a cap must overcount, not undercount.
- The cap (`LLM_BUDGET_USD`, default $5) is lifetime, across all runs, because the
  ledger is durable in Postgres.
- Unknown paid models have no price and are refused (fail closed).
- Local providers are recorded at $0 and are allowed even after the cap is reached.

## Consequences
- Worst-case reservation can refuse a call that would actually have fit. Callers should
  set realistic `max_output_tokens`.
- Prices are hard-coded and must be checked before any paid run.
- Actual cost can exceed the estimate if the prompt-token count was underestimated, so
  total spend can end slightly above the cap; every later paid call is refused.
- Resetting the budget means deleting ledger rows deliberately; there is no API for it.
