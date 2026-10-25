"""Generate an outreach script, check it, revise once, else route to human review."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from caregap import observability
from caregap.llm.base import LLMClient, Message
from caregap.outreach.compliance import ComplianceResult, MemberContext, check
from caregap.outreach.profile import OutreachProfile
from caregap.prompts import PromptStore

COMPLIANT = "compliant"
NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class Attempt:
    text: str
    compliance: ComplianceResult
    input_tokens: int
    output_tokens: int
    latency_s: float
    cost_usd: Decimal = Decimal(0)
    compliance_s: float = 0.0


@dataclass(frozen=True)
class ScriptOutcome:
    member_id: str
    measures: tuple[str, ...]
    language: str
    status: str  # COMPLIANT or NEEDS_REVIEW
    attempts: tuple[Attempt, ...]
    prompt_versions: dict[str, str]
    provider: str
    model: str

    @property
    def script(self) -> str:
        return self.attempts[-1].text

    @property
    def final(self) -> ComplianceResult:
        return self.attempts[-1].compliance

    @property
    def cost_usd(self) -> Decimal:
        """Every model call behind this message, revisions included."""
        return sum((a.cost_usd for a in self.attempts), Decimal(0))


def clean(text: str) -> str:
    """Strip whitespace and quotes models sometimes wrap a whole reply in."""
    text = text.strip()
    if len(text) > 1 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


class ScriptGenerator:
    def __init__(
        self,
        client: LLMClient,
        prompts: PromptStore,
        profile: OutreachProfile,
        *,
        max_output_tokens: int = 600,
    ) -> None:
        self.client = client
        self.prompts = prompts
        self.profile = profile
        self.max_output_tokens = max_output_tokens

    def select_measures(self, open_measures: list[str]) -> tuple[str, ...]:
        """At most ``max_gaps_per_script`` open gaps, in the profile's measure order."""
        ordered = [m for m in self.profile.measure_descriptions if m in open_measures]
        unknown = set(open_measures) - set(ordered)
        if unknown:
            raise ValueError(f"no description for measure(s) {sorted(unknown)}")
        return tuple(ordered[: self.profile.max_gaps_per_script])

    def generate(self, member: MemberContext, open_measures: list[str]) -> ScriptOutcome:
        with observability.span("generate_script", "CHAIN") as span:
            outcome = self._generate(member, open_measures)
            span.set_outputs({"status": outcome.status, "script": outcome.script})
            span.set_attribute("prompt_versions", outcome.prompt_versions)
            span.set_attribute("attempts", len(outcome.attempts))
            span.set_attribute("cost_usd", str(outcome.cost_usd))
        return outcome

    def _generate(self, member: MemberContext, open_measures: list[str]) -> ScriptOutcome:
        measures = self.select_measures(open_measures)
        if not measures:
            raise ValueError(f"member {member.member_id} has no open gaps to script")
        p = self.profile
        prompt = self.prompts.get(p.script_prompt)
        # Only allowlisted member fields reach the model.
        member_fields = {"given_name": member.given_name or "there"}
        messages = prompt.render(
            plan_name=p.plan_name,
            max_words=p.max_words,
            disclaimer=p.disclaimer,
            opt_out=p.opt_out,
            services="\n".join(f"- {p.measure_descriptions[m]}" for m in measures),
            **{k: v for k, v in member_fields.items() if k in p.allowed_member_fields},
        )
        versions = {p.script_prompt: prompt.version}
        purpose = f"outreach-script:{member.member_id}"

        first = self._attempt(messages, member, purpose)
        attempts = [first]
        if not first.compliance.passed:
            revise = self.prompts.get(p.revise_prompt)
            versions[p.revise_prompt] = revise.version
            feedback = "\n".join(f"- {v.rule}: {v.detail}" for v in first.compliance.violations)
            messages = [
                *messages,
                Message("assistant", first.text),
                *revise.render(violations=feedback, disclaimer=p.disclaimer, opt_out=p.opt_out),
            ]
            attempts.append(self._attempt(messages, member, f"{purpose}:revise"))

        return ScriptOutcome(
            member_id=member.member_id,
            measures=measures,
            language=p.language,
            status=COMPLIANT if attempts[-1].compliance.passed else NEEDS_REVIEW,
            attempts=tuple(attempts),
            prompt_versions=versions,
            provider=self.client.provider.name,
            model=self.client.model,
        )

    def _attempt(self, messages: list[Message], member: MemberContext, purpose: str) -> Attempt:
        c = self.client.complete(
            messages, max_output_tokens=self.max_output_tokens, purpose=purpose
        )
        text = clean(c.text)
        with observability.span("compliance", "GUARDRAIL") as span:
            started = time.monotonic()
            result = check(text, self.profile, member)
            compliance_s = time.monotonic() - started
            span.set_outputs({"passed": result.passed,
                              "violations": [v.rule for v in result.violations]})  # fmt: skip
        return Attempt(
            text=text,
            compliance=result,
            input_tokens=c.input_tokens,
            output_tokens=c.output_tokens,
            latency_s=c.latency_s,
            cost_usd=c.cost_usd,
            compliance_s=compliance_s,
        )
