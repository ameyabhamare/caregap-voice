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
- Phases 0-7 are approved. Phase 8 (LLM-as-a-Judge, prompt iteration, CI gate) is built,
  its report is posted, CI is green on the branch (GitHub Actions runs 4 and 5).
- **Waiting on the owner**: they are labeling the 50 scripts in
  `fixtures/judge/calibration.csv` (columns `clarity`, `tone`, `no_medical_advice`, each
  `y` = pass or `n` = fail; `notes` optional). They may send labels back as:
  a. an attached CSV - copy it over `fixtures/judge/calibration.csv` (check: 50 rows, same
     ids and scripts, labels only `y`/`n`);
  b. chat lines like `cal-01: y y n` (order: clarity, tone, no_medical_advice) - write
     them into the CSV with `caregap.evals.calibration.read/write`;
  c. they ran `make label-judge` themselves and pushed.
  Never pre-fill, guess or suggest labels. If something is missing or ambiguous, ask.
- **Then**: commit the labels, run `make judge-agreement` (needs the stack and Ollama),
  and report per criterion: accuracy, Cohen's kappa, and the confusion counts (MLflow
  experiment `judge-calibration`). For each criterion with kappa >= 0.4, add it to
  `GATED_JUDGE_CRITERIA` in `src/caregap/evals/ci.py`, run `make record-fixtures`
  (about 2 hours), review the new thresholds, commit `fixtures/ci` and `reports/`, push,
  and confirm CI is green.
- **Open question for the owner** (ask after the agreement report): outreach-script v2
  ("three sentences" fix) was rejected only because judged medical-advice failures rose
  57 -> 63, and those looked like judge false positives on refill reminders. If the
  medical-advice criterion's kappa is low, should v2 be re-decided without that guard?
  Do not re-decide it without their answer.
- After "approved": mark Phase 8 approved in PROGRESS.md and this file, then post the
  Phase 9 plan (MCP server: get_member_gaps, search_benefits, log_interaction,
  escalate_to_human; Claude Desktop config) and wait for "go".

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
