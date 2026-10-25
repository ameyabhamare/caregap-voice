"""LLM-as-a-Judge for outreach scripts: clarity, tone and no medical advice, pass/fail.

Every call goes through ``LLMClient`` (metered). The judge asks for JSON and falls back
to finding ``<criterion>: pass|fail`` in the reply; a criterion it cannot read is None
("unparsed"), counted separately, never as a pass.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from caregap.llm.base import LLMClient
from caregap.prompts import PromptStore

JUDGE_PROMPT = "outreach-judge"
CRITERIA = ("clarity", "tone", "no_medical_advice")
#: Criteria whose agreement with owner labels is good enough (Cohen's kappa >= 0.4,
#: make judge-agreement) to block a prompt promotion or fail CI. The others are reported
#: only. Owner calibration 2026-10-24 (MLflow judge-calibration 8c7d1295): none qualify.
TRUSTED_CRITERIA: tuple[str, ...] = ()


@dataclass(frozen=True)
class Verdict:
    #: criterion -> True (pass), False (fail) or None (could not parse the judge)
    scores: dict[str, bool | None]
    reason: str
    raw: str
    prompt_version: str

    @property
    def passed(self) -> bool:
        return all(self.scores.get(c) is True for c in CRITERIA)


def parse(reply: str) -> tuple[dict[str, bool | None], str]:
    scores: dict[str, bool | None] = dict.fromkeys(CRITERIA)
    reason = ""
    m = re.search(r"\{.*\}", reply, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            for c in CRITERIA:
                v = str(data.get(c, "")).strip().lower()
                scores[c] = True if v == "pass" else False if v == "fail" else None
            reason = str(data.get("reason", ""))
    for c in CRITERIA:
        if scores[c] is None:
            hit = re.search(rf"{c}\W+(pass|fail)", reply, re.IGNORECASE)
            if hit:
                scores[c] = hit.group(1).lower() == "pass"
    return scores, reason


class Judge:
    def __init__(self, client: LLMClient, prompts: PromptStore, *, max_output_tokens: int = 200):
        self.client = client
        self.prompts = prompts
        self.max_output_tokens = max_output_tokens

    def judge(self, script: str, *, purpose: str = "judge") -> Verdict:
        prompt = self.prompts.get(JUDGE_PROMPT)
        reply = self.client.complete(
            prompt.render(script=script), max_output_tokens=self.max_output_tokens,
            purpose=purpose,
        ).text  # fmt: skip
        scores, reason = parse(reply)
        return Verdict(scores, reason, reply, prompt.version)
