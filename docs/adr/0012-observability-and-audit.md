# 12. Observability and governance: MLflow traces, an append-only audit log, a dashboard

Date: 2026-10-24. Status: Accepted.

## Context
The brief asks for:
- MLflow tracing across generation, compliance, TTS and RAG;
- cost per message, latency p50/p95 per stage, and WER;
- an audit log for every generated message (prompt version, model, eval scores, approver);
- a small dashboard.

Acceptance: one campaign run is traceable from member to audio to outcome.

A campaign member's journey spans several HTTP calls from n8n and Twilio (prepare,
review, send, TwiML, callbacks), minutes or days apart. So no single trace covers it.

## Decision
- **Two layers, linked by trace id.**
  - MLflow traces show what happened inside one operation: prompts, model calls, the
    compliance check, retrieval, the grounding check, TTS.
  - The Postgres audit log shows the journey across operations.
  - Scripts, audio and callback answers store the trace id of the operation that made
    them. The audit events carry it as well.
- **Traces** (`caregap.observability`, experiment `caregap-traces`):
  - There is one root trace per core operation: `prepare` (and `create_script`), `tts`,
    `callback_question` and `benefit_search`.
  - Each trace is tagged with customer, campaign, member and script.
  - Model calls and embeddings are child spans only. A call outside a trace starts none,
    so eval runs (thousands of calls) add no traces.
  - Tracing is controlled by `MLFLOW_TRACING`: on in compose, always off in tests and CI.
  - With MLflow down, tracing turns itself off. A span failure never fails a request.
- **Audit log** (`caregap.audit`, table `audit_log`):
  - It is append-only: a trigger rejects UPDATE and DELETE.
  - It has no foreign keys, so audit rows outlive what they describe.
  - Times come from the database's real clock, even when campaigns run on the demo clock.
  - Each event is written with the connection of the change it records, so both commit
    or neither does.
  - Events:
    - `script_generated`: prompt versions, model, rule results per attempt, reading
      grade, cost, trace id;
    - `script_reviewed`: the approver is the actor;
    - `audio_rendered`;
    - `call_queued`, `call_dispatched`, `call_failed_to_dispatch`, `call_finished`;
    - `dtmf`;
    - `escalation_created`;
    - `callback_answered`;
    - `interaction_logged`.
  - Free text (questions, agent notes, summaries) stays in its own table. The audit log
    holds IDs and outcomes.
  - The timeline is available as `GET /audit/campaigns/{id}/members/{member_id}`, as
    `make trace-member`, and on the dashboard.
- **Eval scores per message** are the deterministic rule results and the computed
  reading grade. The LLM judge is not run per message: it is uncalibrated (ADR 0010) and
  would double the model calls. Judge scores stay in the eval runs.
- **Cost per message:**
  - The ledger reports what it charged for each call.
  - Scripts store the sum over all attempts (`outreach_scripts.cost_usd`), and callback
    answers the sum of route plus answer (`callback_questions.cost_usd`).
  - Both are `NUMERIC`, read as Decimal. Local models cost $0, but the column is still
    filled.
- **Latency** comes from a `stage_latency` view over existing columns, one row per
  measurement:
  - generate: model time;
  - compliance: new `compliance_ms`;
  - tts;
  - call: queued to final status, on the campaign clock;
  - callback_answer.

  The dashboard computes p50 and p95 with `percentile_cont`. WER comes from the voice eval,
  shown from the committed reports.
- **Dashboard:**
  - Streamlit, pinned, in its own `dependency group`. It runs on the host with
    `make dashboard`, not as a compose service, and is left out of the core image.
  - Tabs: campaign funnel, eval pass rates (reports plus MLflow prompt iterations), cost
    (budget, per message, per purpose), latency per stage, and "trace a member" (the
    audit timeline with MLflow trace links).
  - Queries live in `caregap.dashboard.data` and are tested without Streamlit. The app
    has a smoke test with Streamlit's `AppTest`.

## Consequences
- Traces hold prompt text, which includes allowed member fields (Sunrise: first name).
  MLflow is local and the data synthetic, so there is no trace retention or redaction
  yet. With real data, traces need the same review as ADR 0011's trust boundary.
- TRUNCATE is still possible on `audit_log` (tests use it). Append-only protects against
  application bugs, not a database owner.
- Rows from before Phase 10 have no cost, compliance time or trace id. The dashboard
  leaves them out of the cost-per-message figures.
- The core service does not apply schema changes itself: `make db-schema` on existing
  volumes.
