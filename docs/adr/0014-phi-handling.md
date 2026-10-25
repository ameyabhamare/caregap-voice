# 14. PHI handling

Date: 2026-10-25. Status: Accepted.

## Context
CareGap Voice works on the kind of data that is protected health information (PHI) in
a real health plan: names, phone numbers, birth dates, diagnoses, prescriptions and care
gaps. The brief allows synthetic data only, but the design should already behave as if
the data were real, so the rules are tested before anyone is tempted to use real data.
The rules used to be spread over ADRs 0003, 0005, 0007, 0011, 0012 and 0013. This
ADR collects them in one place.

## Decision
- **Synthetic data only.** Members come from Synthea with pinned seeds (ADR 0003).
  Records we add ourselves (edge cases, simulated pharmacy fills) carry the
  `urn:caregap:synthetic` meta tag. Generated data lives under `data/`, which git
  ignores. Fixtures in the repository (edge cases, golden Q&A, judge calibration, CI
  recordings) are synthetic too.
- **Minimum necessary at the model input.** A prompt may only use the fields in the
  customer's `allowed_member_fields` (Sunrise: `given_name`; Horizon: none). Config
  validation checks every configured prompt's variables against that list. Prompts get
  plain descriptions of the open services, never diagnoses, drug names, dates or
  identifiers (ADR 0005).
- **Checked at the output.** Deterministic compliance rules reject scripts that contain
  the member's family name, birth date, age, phone or member ID; anything that looks like
  a date, phone number or ID; diagnosis terms; or drug names, in any supported language.
  No script reaches TTS or a call without passing them or being approved by a person.
- **Callback answers come from documents, not member records.** The callback agent sees
  the plan's Summary of Benefits only. It refuses member-specific questions ("what is my
  balance") and hands them to a person (ADR 0009).
- **IDs only in notifications and audit payloads.** Escalations and alerts sent through
  n8n carry internal IDs (campaign, member, attempt), never names, numbers or health
  details. Agent free text goes in `escalations.note`, which is never sent (ADRs 0007,
  0011). Audit payloads hold IDs and outcomes (ADR 0012).
- **Agents see what the plan allows.** The MCP tools return only `allowed_member_fields`
  and the plan's measures, never phone numbers, family names or birth dates (ADR 0011).
- **Traces carry what prompts carry.** MLflow spans hold the allowed member fields,
  measures, script text and callback questions and answers, not full member records.
  Tracing is off in tests and CI.
- **Leaving the machine is opt-in.** The default stack is fully local. In live mode the
  only recipients are Twilio, Cloudflare (tunnel and R2) and, for the comparison eval,
  Anthropic, as listed in ADR 0013. A member's synthetic phone number is never sent: live
  calls go only to the owner's verified number.
- **CI never sees live data.** It replays recorded model responses and calls no model or
  outside service.

## Consequences
- None of this makes the system ready for real PHI. Real use would need at least:
  - business associate agreements with every outside service that handles PHI (Twilio,
    Cloudflare or another storage and tunnel provider, the LLM provider);
  - encryption at rest for Postgres, MLflow artifacts and `data/audio/`;
  - access control and authentication on the core API, n8n, MLflow and the dashboard
    (today they rely on binding to localhost);
  - retention and deletion rules for audio, recordings, traces and the audit log;
  - a review of the clinical detail in care-gap reasons, which agents can see.
- The output rules are pattern and word lists. They catch the identifiers we know about,
  but a model could still invent a statement about a member's health without a listed
  term in it. The human-review queue and the (uncalibrated) judge are the backstops.
- Synthetic members still look real (names, addresses, phone numbers). Screenshots and
  recordings should say they are synthetic, as the README does.
