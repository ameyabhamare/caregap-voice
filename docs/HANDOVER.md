# Hand-over prompt

Paste this into a new Claude Code session on branch `claude/festive-mendel-i76y7j`
(PR: https://github.com/ameyabhamare/caregap-voice/pull/1).

---

You are continuing the CareGap Voice project. Before doing anything else, read, in order:
1. `docs/BRIEF.md` - the owner's brief and the strict working agreement (plan -> "go" ->
   build -> phase report -> "approved"; one phase at a time; ask before new dependency
   categories, paid services or infrastructure).
2. `CLAUDE.md` - conventions, layout, commands.
3. `PROGRESS.md` - phase status, decisions log, open questions, and the "Resume here"
   section with environment quirks and how to rebuild local state.
4. `docs/adr/` - decisions so far (0001-0006).

State: Phases 0-4 are approved. Your next action is to **post the Phase 5 plan
(orchestration: n8n + Twilio mock) and wait for "go"**. Do not write Phase 5 code before
then.

Things the owner has told us that are not obvious from the repo:
- Reports use the five-section format in BRIEF.md; be concise and honest, including when
  something got worse (it is recorded, not hidden - see the Phase 4 dictionary v1 run).
- The prompt-v1 bug (says "two sentences", supplies three) is deliberately kept as the
  Phase 8 baseline; fixing it is Phase 8's first measured iteration.
- Default approvals so far were "defaults are fine" on each plan's listed decisions.
- Keep an eye on context size; write a new hand-over (update this file) at a natural
  boundary when it gets large.

Environment (cloud container, ephemeral):
- If `data/` or Docker volumes are gone, rebuild per PROGRESS.md "Resume here":
  start dockerd, `make up`, `make seed` (~18 min), `make pipeline`, `make sync-gaps`,
  `make prompts-register`, put qwen2.5:3b into Ollama (fetch manifest + blobs from
  registry.ollama.ai on the host and `docker cp` into `/root/.ollama/models/`; containers
  have no outside DNS). Kokoro files download on first use; Whisper small.en downloads from
  Hugging Face on first eval.
- Network allowlist already includes Databricks workspace, registry.ollama.ai,
  *.r2.cloudflarestorage.com, huggingface.co, *.huggingface.co, *.hf.co. New hosts need
  the owner to add them (environment settings -> Network access); never ask for tokens in
  chat. The Databricks token is injected by a network secret; `.env` needs
  `DATABRICKS_HOST=https://dbc-02701db1-ff8f.cloud.databricks.com` and a placeholder token.
- Docker Hub can rate-limit pulls; containers cannot resolve DNS (do downloads on host).

Latest eval numbers (MLflow, local): rule eval first-attempt pass 28%, final 65%
(run 6faee595, all failures = dropped disclaimer sentence); voice: script WER 2.3%,
medication names 54.5% -> 88.6% with overrides (run 1431b6ee).
