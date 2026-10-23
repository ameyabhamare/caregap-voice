# 7. Orchestration: n8n as a thin scheduler, a Twilio mock, decisions in the core service

Date: 2026-10-23. Status: Accepted.

## Context
Outreach runs as campaigns: pick members with open gaps, generate and check a script,
have a person approve the first messages, call inside the allowed hours, retry calls
that did not connect, honour opt-outs, and tell a person when something needs them. The
brief puts orchestration in self-hosted n8n with no business logic in it, telephony
behind a local Twilio mock by default, and a real Twilio trial only in Phase 11.

## Decision
- **Every decision lives in the core service** (`caregap.campaign.service`): who is
  selected and why others are skipped, consent, the call window, the first-N approval
  gate, retry policy, what DTMF digits mean, when a person is alerted. It is a per-member
  state machine in Postgres (`campaign_members`, `call_attempts`, `telephony_events`,
  `escalations`). n8n can be replaced by cron and curl without losing any rule.
- **n8n does orchestration only**: schedules (every minute) and webhooks; split, route
  and call HTTP endpoints; HTTP retries; delivery of notifications; an error workflow.
  Four workflows, exported as JSON in `n8n/workflows/`. A test allows only trigger,
  HTTP, split, switch/if and respond nodes, and only the core URL or URLs that the core
  service hands out, so no Code node or business rule can slip in.
- **Idempotent work claims.** `GET /work` lists what is due. The action endpoints
  (`prepare`, `send`) change state with conditional updates under row locks, so
  overlapping n8n executions cannot generate twice or place two calls. Work stuck in
  `preparing` (15 min) or `calling` (30 min, a lost callback) is recovered by `/work`.
- **n8n places the call.** The core service returns the complete Twilio request (URL,
  auth header, form). n8n posts it, with HTTP retries, and reports the call SID or the
  failure. This keeps the Phase 11 shape (n8n hands Twilio a signed audio URL) and makes
  delivery visible in n8n's execution log.
- **Twilio callbacks go to n8n**, which relays query, form body and signature to the core
  service. The core service checks `X-Twilio-Signature` against the URL **it issued**
  (rebuilt from settings and the attempt ID), not the URL n8n reports, so the relay cannot
  change what was signed. TwiML fetches are signed and checked the same way.
- **Twilio mock** (`caregap.telephony.mock`) implements the parts of Twilio's wire format
  we use: `Calls.json` with basic auth, signed form posts, TwiML `<Play>`/`<Gather>`, E.164
  validation and error codes. For answered calls it fetches the TwiML and the audio and
  checks it is a WAV. Outcomes are deterministic from (number, call count), with a fixed
  distribution (55% completed, 18% no answer, 6% busy, 4% failed, 7% opt-out, 6% ask for a
  person, 4% HTTP 503), and can be scripted per number for tests.
- **Consent**: no row means unknown, and unknown means no call. Synthea has none, so
  members get deterministic synthetic consent (90% granted, 5% revoked, 5% unknown).
  Consent is checked at selection and again when the call is placed; DTMF 9 revokes it.
- **First-N approval** reuses the Phase 3 review queue: compliant scripts among the first
  `approval_first_n` of a campaign are stored as `pending_approval`. Scripts that failed
  compliance are always reviewed and count toward N.
- **Call window** in the member's local time (profile: Mon-Sat 09:00-20:00 New York,
  stricter than the TCPA's 8am-9pm). Retries move to the next window opening.
- **Alerts and escalations use an outbox** (`escalations.notified_at`). n8n delivers to a
  Slack-compatible webhook (`NOTIFY_WEBHOOK_URL`; default: the core service's own log
  sink) and marks rows delivered, so a channel outage delays notifications without losing
  them. Notifications carry internal IDs only: no names, phone numbers or health details.
- **n8n 1.121.3, SQLite in a volume.** 1.122+ added workflow versioning, and its CLI
  cannot activate a workflow it imported (foreign key on `activeVersionId`; no version
  history row is created). 1.121.3 imports and activates from the CLI, so `make
  n8n-import` needs no user account or API key. n8n keeps only workflows and execution
  history, so SQLite is enough and avoids creating another database on an existing
  Postgres volume.
- **The core service runs in compose** (`docker/core/Dockerfile`, no dev or PySpark
  packages). n8n and the mock reach it by service name; it runs as the host user so files
  under `data/` stay yours.

## Consequences
- The demo needs a clock inside the call window: `CAMPAIGN_CLOCK` starts the service clock
  at a fixed instant and lets it run. The clock restarts with the container.
- Twilio auth travels through n8n's execution data (the core service returns the header).
  Fine for the mock. With a real account (Phase 11) the credential moves into an n8n
  credential.
- n8n's schedule triggers run every minute, so a campaign's natural pace is one tick per
  minute; `make run-campaign` triggers the webhooks to go faster.
- Upgrading n8n past 1.121 needs an activation path that creates version history (n8n's
  public API with an API key). Revisit when the CLI supports it.
