# 10. Evals: a calibrated judge, measured prompt iteration, a fixture-based CI gate

Date: 2026-10-23. Status: Accepted.

## Context
Rule checks catch missing disclaimers and leaked PHI but not whether a script is clear,
kind or quietly giving medical advice. Prompts need to improve without silently getting
worse, and CI (a public GitHub repository) must never call a paid API or a large model.

## Decision
- **Judge.** An LLM judge (`outreach-judge` prompt, local `qwen2.5:3b` by default) rates
  clarity, tone and no-medical-advice pass/fail with a one-line reason. An unreadable
  verdict is "unparsed", never a pass. Reading level is computed (Flesch-Kincaid,
  grade 8 or below, English only), not judged: a formula needs no calibration.
- **Calibration.** 50 distinct English scripts from earlier eval runs, half of them
  scripts that failed compliance, with empty label columns. The owner labels them
  (`make label-judge` or any spreadsheet); nothing pre-fills or suggests labels.
  `make judge-agreement` reports accuracy and Cohen's kappa per criterion to MLflow. A
  criterion gates CI or prompt promotion only once its kappa is 0.4 or more
  (`judge.TRUSTED_CRITERIA`); until then it is reported. Owner calibration on 2026-10-24:
  none qualify.
- **Prompt iteration.** `make prompts-candidate` registers changed prompts under a
  `candidate` alias; `make eval ALIAS=candidate` runs the suite (Sunrise, Sunrise
  Spanish, Horizon: rules + judge + reading level) as one MLflow run with a child per
  configuration and compares it with the last promoted run. Promote only if the mean
  final compliance pass rate improves, no guard metric drops more than 3 points and
  judged medical-advice failures do not rise; judge-based guards count only for trusted
  criteria (amended 2026-10-24, owner decision). Promotion moves `production`; a rejected
  candidate leaves production alone and its file is restored. Configurations whose
  inputs did not change are reused from earlier runs.
- **CI without models.** `LLM_FIXTURES=record` saves every chat and embedding response,
  keyed by a hash of the whole request; `replay` serves only those and raises on
  anything else. `make record-fixtures` records the full suite and callback Q&A (with the
  sampled synthetic members) and derives thresholds; `make eval-ci` replays them in
  GitHub Actions with only a Postgres service. Any change to a prompt or to how requests
  are built shows up as a missing recording, so fixtures are re-recorded deliberately and
  the new numbers reviewed.
- **Reports.** `reports/<customer>.md` and `.json` (scripts, judge, reading level,
  callback Q&A, voice) are written by recording (live) and by CI (replay, as artifacts).

## Consequences
- A 3B judge is weak; its agreement with the owner decides how much it may gate.
- Recorded fixtures pin model behavior at recording time: CI checks the code and prompts
  against that behavior, not the model itself. Re-recording is a reviewed change.
- The committed fixtures include synthetic member contexts (Synthea names and dates) for
  the sampled members, so CI needs no member database.
- Spanish scripts are judged (the rubric works in Spanish) but are not in the calibration
  set and have no reading-level score.
