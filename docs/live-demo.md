# Live demo: one real call to your phone

Live mode is optional and off by default. It uses:
- a free **Twilio trial** account, whose calls are paid from trial credit;
- a free-tier **Cloudflare R2** bucket;
- a free **Cloudflare quick tunnel** (no account needed);
- optionally your **Anthropic API key**, for the Claude comparison, capped by
  `LLM_BUDGET_USD`.

Run it on your own computer. Put secrets only in your local `.env`, never in a commit or
a chat. Every member is synthetic. Calls go only to the number you verify, and the
design is in [ADR 0013](adr/0013-live-mode-trust-boundary.md).

## 0. The local stack works

```bash
make up && make seed && make pipeline && make sync-gaps
make prompts-register && make ingest-benefits
make run-campaign            # mock calls; must finish before going live
```

## 1. Twilio trial
1. Sign up at twilio.com. Trial accounts can call only verified numbers.
2. Get a trial phone number. This is your `TWILIO_FROM_NUMBER`.
3. Verify your own phone: Phone Numbers → Manage → Verified Caller IDs.
4. In the Console, copy the Account SID and Auth Token.

Trial calls start with Twilio's notice and ask you to press a key; then our message plays.

## 2. Cloudflare R2
1. In the Cloudflare dashboard, enable R2. It may ask for a payment method even on the
   free tier.
2. Create a bucket, for example `caregap-audio`.
3. In the bucket's settings, add an object lifecycle rule that deletes objects 1 day
   after upload.
4. Under R2 → Manage API tokens, create a token with **Object Read & Write** on **that
   bucket only**.
5. Copy the Access Key ID, the Secret Access Key and the S3 endpoint
   (`https://<account_id>.r2.cloudflarestorage.com`).

## 3. `.env`

```bash
LIVE_MODE=true
LIVE_ALLOWED_NUMBERS=+1XXXXXXXXXX        # your verified number, E.164
LIVE_DEMO_NUMBER=+1XXXXXXXXXX            # the same number
LIVE_MAX_CALLS=3
TWILIO_API_BASE_URL=https://api.twilio.com
TWILIO_ACCOUNT_SID=AC...
TWILIO_AUTH_TOKEN=...
TWILIO_FROM_NUMBER=+1YYYYYYYYYY          # your Twilio trial number
AUDIO_STORE=r2
R2_ENDPOINT=https://<account_id>.r2.cloudflarestorage.com
R2_BUCKET=caregap-audio
R2_ACCESS_KEY_ID=...
R2_SECRET_ACCESS_KEY=...
```

## 4. Start the tunnel

```bash
make live-up
```

This does three things:
- starts the edge filter and the tunnel;
- writes `LIVE_WEBHOOK_URL` (the tunnel's webhook URL) to `.env`;
- restarts the core service.

If a setting is missing, the core service will not start; `docker compose logs core`
names the missing setting. The tunnel URL changes on every `make live-up`.

## 5. The call

```bash
make live-call               # CUSTOMER=horizon for the other plan
```

1. n8n prepares one member's script, and the command shows it to you.
2. Answer `y` to approve it. That places the real call to `LIVE_DEMO_NUMBER`.
3. Your phone rings. Press a key for Twilio's trial notice, then listen.
4. While the message plays, press 9 to opt out, 0 to ask for a call back, or nothing.

When the campaign is done, the command prints its number. Then:

```bash
make live-recording CAMPAIGN=<number>      # saves docs/demo/live-call-campaign-<n>.mp3
make trace-member CAMPAIGN=<number> MEMBER=<member>   # or the dashboard's "Trace a member"
```

The recording has Twilio's trial notice, the synthetic message, and any keys you
pressed. Listen to it before committing it for the README.

## 6. Inbound callback calls (optional)
1. `make live-up` prints a URL per plan.
2. In the Twilio Console, open your number, set "A call comes in" to **Webhook, HTTP
   POST**, and paste the Sunrise URL.
3. Call your Twilio number from your verified phone and ask, for example: "How much is
   a visit to a specialist?" The answer comes only from the plan's Summary of Benefits,
   after the grounding check.
4. Medical questions are handed to a person. Press 0 to ask for a call back.

## 7. Claude vs the local model (optional)
Put `ANTHROPIC_API_KEY=...` in `.env` and run:

```bash
make eval-compare COMPARE_ARGS=--precheck-only   # about $0.05; prints the projected total
make eval-compare                               # Sonnet by default; about $3
```

It stops by itself if the projection is over the budget left. The result is
`docs/results/claude-vs-local.md`.

## 8. Turn it off

```bash
make live-down
```

Then:
1. In `.env`, set `LIVE_MODE=false` and `AUDIO_STORE=core`, and delete
   `LIVE_WEBHOOK_URL`. Then run `make up`.
2. Optionally:
   - delete the call recording in the Twilio Console;
   - revoke the R2 token;
   - clear the Twilio number's webhook.

## Troubleshooting
- **"An application error has occurred" on the call:**
  - Run `docker compose logs edge n8n core`.
  - The most common cause is a stale tunnel URL: run `make live-up` again, and update
    the inbound webhook.
- **Signature errors (403 in the core logs):** `LIVE_WEBHOOK_URL` does not match the URL
  Twilio called.
- **The audio never plays:**
  - Check the R2 token's scope and that the bucket name matches.
  - n8n's "Upload audio (R2)" node shows the error, and the attempt is retried.
- **Inbound answers time out:** Twilio waits about 15 s. The local model is slower on
  some questions; the Claude provider is faster.
- **`make run-campaign` in live mode:** it expects the Twilio mock. Use `make live-call`
  instead.
