# Hand-over prompt

Paste everything below the line into a new Claude Code session on branch
`claude/festive-mendel-i76y7j` (PR: https://github.com/ameyabhamare/caregap-voice/pull/1).

---

You are continuing the CareGap Voice project: a voice outreach agent for health plan care
gaps, built phase by phase for an owner who approves each phase. Before doing anything
else, read, in order:
1. `docs/BRIEF.md` - the owner's brief and the strict working agreement: post a plan, wait
   for "go", build, post a phase report (five sections), wait for "approved". One phase at
   a time. Ask before any new dependency category, paid service or infrastructure.
2. `CLAUDE.md` - conventions, layout, commands. It is current; follow it.
3. `PROGRESS.md` - phase checklist, decisions log, open questions, and "Resume here"
   (environment quirks, how to rebuild local state).
4. `docs/adr/` 0001-0014, especially 0010 (judge, prompt iteration, CI gate), 0013 and 0014
   (what may leave the machine; PHI).

## Where things stand
- Phases 0-11 are approved (Phase 11 on 2026-10-25). The Claude comparison ran on
  2026-10-25 (sonnet-5-5, $0.94 of the $5 cap; docs/results/claude-vs-local.md, README
  Results). The owner deferred the real call and its recording to later, on their own
  computer.
- Next: Phase 12 (docs and hand-off). Its plan is posted; wait for "go".
- The ledger has $0.94 of paid spend recorded; a fresh Postgres volume resets it, so
  the cap is per volume. Never ask for an API key in chat. This container's `.env` holds
  a key the owner pasted on 2026-10-25 and was advised to revoke; if Claude calls fail
  with 401, it has been revoked (expected).
- Judge calibration: no criterion reaches kappa 0.4, so `judge.TRUSTED_CRITERIA` is empty.
  Never pre-fill, guess or suggest judge labels.

## Phase 11 facts worth knowing
- This environment reaches api.anthropic.com but not Twilio, R2 or trycloudflare.com.
- `caregap.live.problems()` fails startup in live mode; `LiveConfig` in the campaign
  service enforces demo number, cap (advisory lock) and one-member campaigns
  (`campaigns.live`). `callback_url(attempt, event)`: event status | gather | twiml.
- `caregap.telephony.inbound.InboundCalls`: event inbound | inbound-speech, query
  customer + turn (signed). Greeting/qa_more in customers/*.yaml must name the callback
  digit (validated).
- Fake R2 lives in the Twilio mock at /r2/...; to try it locally export AUDIO_STORE=r2,
  R2_ENDPOINT=http://twilio-mock:8100/r2, R2_BUCKET, R2_ACCESS_KEY_ID,
  R2_SECRET_ACCESS_KEY before `docker compose up -d core twilio-mock` (don't edit .env).
- The core image was rebuilt with Phase 11 code on 2026-10-24. Disk is tight: prune
  dangling images and builder cache after each rebuild.

## Phase 10 facts worth knowing
- `caregap.observability`: root spans only where `root=True` (prepare, create_script,
  tts, callback_question, benefit_search); model calls/embeddings are child spans only.
  `MLFLOW_TRACING` (Settings default false; compose and .env true). tests/conftest.py
  forces it off because `make` exports .env. Experiment `caregap-traces`.
- `caregap.audit`: `audit_log` (append-only trigger, no FKs, `at` = DB clock_timestamp).
  `audit.record(conn, event, actor=..., payload=..., **refs)` with the change's own
  connection. `timeline(conninfo, campaign_id, member_id)`; `make trace-member`.
- Costs: `Usage.actual_usd` from the ledger -> `Completion.cost_usd` ->
  `Attempt.cost_usd`/`ScriptOutcome.cost_usd` -> `outreach_scripts.cost_usd`;
  `Answer.cost_usd` -> `callback_questions.cost_usd`. `stage_latency` view for p50/p95.
- Dashboard: `src/caregap/dashboard/app.py` (Streamlit 1.65.0, `dashboard` group, not in
  the core image), queries in `data.py`; AppTest smoke test in
  tests/integration/test_dashboard.py.
- The core image was rebuilt with Phases 9-10 on 2026-10-24; dev DB has the schema.

## Phase 9 facts worth knowing
- `caregap.mcp` (stdio, `mcp` SDK 2.3.0: `MCPServer`, in-process `Client(server)` for
  unit tests, `Client(StdioServerParameters(...))` for the end-to-end test) calls the core
  API; rules in `caregap.members`. Tools: get_member_gaps, search_benefits,
  log_interaction, escalate_to_human. ADR 0011, docs/mcp.md, `make mcp-config`.
- Schema: `member_interactions`; `escalations.note` (agent text; `detail` stays IDs-only);
  kind `agent_request` with a partial unique index (member, md5(note)) while open. The
  escalation kinds are defined once in schema.sql (the Phase 7 block): every kind ever
  written must be listed there, because the schema is re-applied on every start.
- The core service does not apply the schema itself: `make db-schema` on old volumes
  (done on this container's dev database). The running `core` container still has the
  Phase 8 image; `make up` rebuilds it (needs the build CA, see Environment).
- Dev database has smoke-test rows: interaction 1 and escalation 10 (agent_request,
  member 017415e5-..., note starts "Smoke test").

## Phase 8 facts worth knowing
- Prompts in production: outreach-script v3 (no health claims; promoted), outreach-judge
  v1, callback-route v3, callback-answer v1, outreach-script-es / -generic / revise
  prompts v1. The YAML files in `prompts/` match production; keep it that way.
- Prompt history (MLflow `eval-suite`): baseline a58b40b3 (mean final 89.3%), v2
  rejected 12229420, v3 promoted e7f68944 (99.7%; Sunrise 68% -> 99% final, 28% -> 92%
  first attempt), v4 rejected 7d8e8e94 (97.3%).
- Judge observations so far (qwen2.5:3b): halo effect (fails clarity and medical advice
  together), flags all Horizon statin-refill scripts as medical advice, passes Spanish
  scripts much less often, and can give different verdicts for identical requests.
- CI fixtures: 381 chat + 108 embedding responses keyed by request hash; replay fails on
  any unrecorded request. Thresholds and reports are derived from a replay of the
  recording. Any change to a prompt or to how requests are built needs a re-record.
- Horizon outreach scripts are near-duplicates (about 5 distinct texts for 72 members):
  no name in its prompt plus a fixed seed.

## How the owner works
- Reports: five sections from BRIEF.md, concise, honest about anything that got worse.
- Defaults have been accepted each phase ("go, defaults work"); still list the decisions.
- They read in the Claude app: send files they need to act on with SendUserFile.
- Before every commit: `make lint` (includes the format check CI runs) and `make check`.
  Push to the branch; check CI with the GitHub MCP tools (`actions_list`,
  `get_job_logs`). Do not open new PRs.
- One logical change per commit; one change per prompt eval; promote only on improvement.

## Environment (cloud container, ephemeral)
- After a restart Docker is down: `nohup dockerd &`, then start the stack with
  `HOST_UID=$(id -u) HOST_GID=$(id -g) docker compose up -d --no-build --wait` (or
  `make up`, which may rebuild the core image; builds need the CA, see below). Volumes and
  `data/` have survived restarts so far; check before rebuilding anything.
- If volumes or `data/` are gone, rebuild per PROGRESS.md "Resume here": `make up`,
  `make seed` (~18 min), `make pipeline`, `make sync-gaps`, `make prompts-register`,
  `make ingest-benefits`; put `qwen2.5:3b` and `nomic-embed-text` into Ollama by fetching
  the manifest and blobs from registry.ollama.ai on the host and `docker cp` into
  `/root/.ollama/models/` (containers have no outside DNS). Losing the Postgres volume
  also loses MLflow history and prompt versions: `make prompts-register` restores the
  current texts as new versions, and the eval-suite baseline must be re-run (`make eval`).
- Docker Hub rate-limits (429): pull from `mirror.gcr.io/...` and `docker tag` to the
  pinned name. Image builds need the proxy CA as a build secret: copy
  `/root/.ccr/ca-bundle.crt` to `docker/extra-ca.pem` and set
  `BUILD_EXTRA_CA_CERT=./docker/extra-ca.pem` in `.env` (both gitignored).
- `.env` (gitignored) also has `CAMPAIGN_CLOCK=2026-01-13T10:00:00-05:00` (demo clock) and
  the Databricks host; the Databricks token comes from a network secret.
- Disk allowance is small (about 3.8 GB free): `docker builder prune -f`,
  `uv cache clean`, `docker image prune -f`; `data/synthea` can be deleted (regenerated by
  `make seed`). Never rebuild the core image from an empty cache when space is low.
- Long runs (`make eval`, `make record-fixtures`, evals) take 1-2 hours on CPU: start
  them with nohup into a log under `/tmp/claude-0/` and poll the log.

## Later phases (from the brief)
9 MCP server; 10 observability + governance (tracing, cost and latency per stage,
audit log, Streamlit dashboard); 11 live demo mode (optional spend, ask before enabling;
also wires inbound callback calls, deferred from Phase 7); 12 docs and hand-off.
Deferred items noted in PROGRESS.md: Spanish round-trip WER (needs multilingual
Whisper), Twilio mock only "presses" 9 or 0 (Horizon opts out with 8).
