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
4. `docs/adr/` 0001-0010, especially 0010 (judge, prompt iteration, CI gate).

## Where things stand
- Phases 0-8 are approved (Phase 8 on 2026-10-24). **Next action: post the Phase 9 plan**
  (MCP server exposing get_member_gaps, search_benefits, log_interaction,
  escalate_to_human; Claude Desktop connection config; acceptance: Claude Desktop can
  inspect a member and trigger an escalation) and wait for "go".
- Judge calibration (owner labels, all 50 in `fixtures/judge/calibration.csv`; MLflow
  judge-calibration 8c7d1295): clarity 66%, kappa -0.08; tone 98%, kappa 0.00; no medical
  advice 100%, kappa n/a. No criterion reaches 0.4, so `judge.TRUSTED_CRITERIA` is empty:
  judge metrics are reported but block neither CI (`ci.derive_thresholds`) nor prompt
  promotion (`suite.decide`). If the judge or its prompt changes, re-run
  `make judge-agreement`; add a criterion only once its kappa is 0.4 or more, then
  `make record-fixtures` and review the new thresholds.
- Never pre-fill, guess or suggest judge labels.

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
