# 13. Live demo mode: the Twilio, R2 and tunnel trust boundary

Date: 2026-10-24. Status: Accepted.

## Context
Phase 11 makes one real call to the owner's verified phone, records it for the README,
answers real inbound calls, and compares Claude with the local model. That means:
- a public webhook for Twilio;
- audio hosted where Twilio can fetch it;
- outside services seeing call data.

The default stack stays fully local at $0. Live mode is opt-in and must fail closed.

## Decision
- **One public route.**
  - A Cloudflare quick tunnel (pinned `cloudflared`, compose profile `live`) publishes
    only `caregap.telephony.edge`.
  - The edge filter forwards `POST /webhook/twilio-events` (query, form body,
    `X-Twilio-Signature`) to n8n and returns 404 for everything else. So the n8n
    editor, the core service, MLflow and Postgres stay private.
  - Every Twilio request goes through that one webhook, which n8n relays to the core
    service, including the TwiML fetch (`event=twiml`) and inbound calls
    (`event=inbound`).
  - The core service checks each signature against the URL rebuilt from its own
    settings, never from the relayed request.
- **Audio through R2, never from the core service.**
  - When a call is claimed, the core service returns a 10-minute presigned PUT URL. n8n
    copies the WAV from the core service to it.
  - The TwiML Twilio fetches contains a 15-minute presigned GET URL.
  - Signing is AWS SigV4, about 60 lines of standard library, verified against AWS's
    published example. No new dependency.
  - A bucket lifecycle rule deletes audio after a day.
  - The mock's fake R2 checks the same signatures, so the path is tested locally.
- **Fail closed** (`caregap.live`):
  - With `LIVE_MODE=true` the core service does not start unless all of these hold:
    - the allowlist and demo number are set (demo number on the allowlist, E.164);
    - `AUDIO_STORE=r2` with complete R2 settings;
    - every Twilio-facing URL uses HTTPS;
    - the Twilio account is not the mock's.
  - At run time:
    - live campaigns have one callable member;
    - every call goes to `LIVE_DEMO_NUMBER`, never the synthetic member's number;
    - at most `LIVE_MAX_CALLS` live calls in total, serialized by an advisory lock;
    - live calls are recorded;
    - a person approves the script in the terminal before the call (`make live-call`).
- **Inbound calls:**
  - Twilio recognises the caller's speech. The answer comes only from the callback
    agent after its grounding check, read out by Twilio's `<Say>`.
  - Up to three questions per call. The callback digit, or any hand-off, escalates with
    IDs only.
- **Claude comparison** (`make eval-compare`):
  - A precheck projects the cost from the ledger and stops above the budget left.
  - The judge stays local.
  - Nothing is promoted.

## What leaves the machine (live mode)
| Recipient | Sees | Kept |
|---|---|---|
| Twilio | the demo number, rendered audio (via R2 URL), TwiML, keypresses, inbound speech transcripts, call recording | per the Twilio account; delete the recording after the README copy |
| Cloudflare tunnel | Twilio's webhook traffic (TLS ends at Cloudflare): call status, digits, inbound transcripts, TwiML with audio URLs | transit |
| Cloudflare R2 | the rendered WAV (synthetic script) | under 24 h (lifecycle rule) |
| Anthropic (eval only) | prompts with allowed member fields (Sunrise: first name), benefit excerpts, golden questions | per API terms |

Every member is synthetic. Live mode never sends a member's synthetic phone number
anywhere.

## Consequences
- With real members this is not acceptable as is. It would need:
  - business associate agreements (BAAs) with Twilio and any storage or tunnel provider
    that handles PHI;
  - a named tunnel with access policies;
  - a review of what the TwiML, recordings and transcripts carry.
- A quick tunnel's URL changes on every `make live-up`. The core service's
  `LIVE_WEBHOOK_URL` and the Twilio number's inbound webhook must follow it, and a stale
  URL fails Twilio's signature check (safe, but the call errors).
- Twilio waits about 15 s for an inbound answer. The local model answers in about 7 s at
  p50, so slow answers can time out (Twilio then plays its error message).
- This environment cannot reach Twilio, R2 or the tunnel service. Live mode is
  exercised here against the mock and the fake R2. The real call runs on the owner's
  computer (docs/live-demo.md).
