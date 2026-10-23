"""Customer configs: one validated YAML file per health plan in ``customers/``.

Everything that differs between customers lives here: who their members are, which care
gaps they call about, wording and disclaimers per language, voices, call windows,
escalation rules and which member fields may reach a prompt. Code reads a config;
adding a customer is a new YAML file.

    python -m caregap.customers        # make validate-config

``CustomerConfig.profile(language)`` gives the per-language view that script generation
and compliance work with (``OutreachProfile``).
"""

from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from caregap.outreach.profile import CampaignPolicy, OutreachProfile

DEFAULT_DIR = Path("customers")
PROMPTS_DIR = Path("prompts")

#: Care-gap measures the pipeline computes (caregap.pipeline.gold.MEASURES).
KNOWN_MEASURES = ("awv", "eye_exam", "statin")
#: Member fields a config may allow into prompts (see outreach.compliance.MemberContext).
MEMBER_FIELDS = ("given_name", "family_name", "birth_date", "age", "phone", "member_id")
#: Variables any prompt may use; member fields come on top, if the config allows them.
PROMPT_VARIABLES = frozenset(
    {"plan_name", "max_words", "disclaimer", "opt_out", "services", "violations"}
)


class LanguageSupport(BaseModel):
    model_config = ConfigDict(frozen=True)
    #: Kokoro voices whose name starts with one of these speak the language.
    voice_prefixes: tuple[str, ...]
    #: espeak language code Kokoro phonemizes with.
    tts_lang: str


#: Languages the platform supports. A config may use any subset.
LANGUAGES: dict[str, LanguageSupport] = {
    "en": LanguageSupport(voice_prefixes=("af_", "am_", "bf_", "bm_"), tts_lang="en-us"),
    "es": LanguageSupport(voice_prefixes=("ef_", "em_"), tts_lang="es"),
}

#: Voices in the pinned Kokoro voices file (a models test checks the file agrees).
KOKORO_VOICES = frozenset(
    {
        *("af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore"),
        *("af_nicole", "af_nova", "af_river", "af_sarah", "af_sky", "am_adam", "am_echo"),
        *("am_eric", "am_fenrir", "am_liam", "am_michael", "am_onyx", "am_puck", "am_santa"),
        *("bf_alice", "bf_emma", "bf_isabella", "bf_lily", "bm_daniel", "bm_fable"),
        *("bm_george", "bm_lewis", "ef_dora", "em_alex", "em_santa"),
    }
)


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MemberRule(_Model):
    """Who the customer's members are: age on December 31 of the measurement year."""

    min_age: int | None = Field(default=None, ge=0)
    max_age: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _check(self) -> MemberRule:
        if self.min_age is not None and self.max_age is not None and self.min_age > self.max_age:
            raise ValueError("members.min_age must not exceed members.max_age")
        return self

    def bounds(self) -> tuple[int, int]:
        return (self.min_age or 0, 200 if self.max_age is None else self.max_age)

    def includes(self, birth_date: date, measurement_year: int) -> bool:
        end = date(measurement_year, 12, 31)
        age = end.year - birth_date.year - ((end.month, end.day) < (birth_date.month,
                                                                     birth_date.day))  # fmt: skip
        lo, hi = self.bounds()
        return lo <= age <= hi


class LanguageConfig(_Model):
    #: Kokoro voice for scripts in this language.
    voice: str
    #: Language of the telephony provider's own speech (TwiML <Say language>).
    say_language: str
    script_prompt: str
    revise_prompt: str
    #: Must appear verbatim in every script, and name the plan.
    disclaimer: str
    #: Must appear verbatim in every script, and name the opt-out digit.
    opt_out: str
    #: Spoken by the telephony provider after the message or a key press.
    goodbye: str
    opt_out_confirmation: str
    callback_confirmation: str
    #: How each measure is described: services, never diagnoses or drugs.
    measure_descriptions: dict[str, str]


class EscalationPolicy(_Model):
    #: Key that opts the member out (revokes voice consent).
    opt_out_digit: str = Field(pattern=r"^[0-9*#]$")
    #: Key that asks for a person to call back.
    callback_digit: str = Field(pattern=r"^[0-9*#]$")
    #: Alert a person when a member could not be reached after every attempt.
    alert_on_delivery_failure: bool

    @model_validator(mode="after")
    def _check(self) -> EscalationPolicy:
        if self.opt_out_digit == self.callback_digit:
            raise ValueError("opt_out_digit and callback_digit must differ")
        return self


class CustomerConfig(_Model):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    plan_name: str
    members: MemberRule
    #: In priority order: a script covers the first ``max_gaps_per_script`` open ones.
    measures: tuple[str, ...] = Field(min_length=1)
    max_words: int = Field(gt=0)
    max_gaps_per_script: int = Field(ge=1)
    #: Member fields a prompt may use. Anything else about the member is PHI to keep out.
    allowed_member_fields: tuple[str, ...]
    default_language: str
    languages: dict[str, LanguageConfig] = Field(min_length=1)
    campaign: CampaignPolicy
    escalation: EscalationPolicy

    @model_validator(mode="after")
    def _check(self) -> CustomerConfig:
        errors: list[str] = []
        if unknown := set(self.measures) - set(KNOWN_MEASURES):
            errors.append(f"unknown measures {sorted(unknown)}; known: {KNOWN_MEASURES}")
        if len(set(self.measures)) != len(self.measures):
            errors.append("measures must not repeat")
        if unknown := set(self.allowed_member_fields) - set(MEMBER_FIELDS):
            errors.append(f"unknown member fields {sorted(unknown)}; known: {MEMBER_FIELDS}")
        if self.default_language not in self.languages:
            errors.append(f"default_language {self.default_language!r} has no languages entry")
        for code, lang in self.languages.items():
            where = f"languages.{code}"
            support = LANGUAGES.get(code)
            if support is None:
                errors.append(f"{where}: unsupported language; supported: {sorted(LANGUAGES)}")
                continue
            if lang.voice not in KOKORO_VOICES:
                errors.append(f"{where}.voice: {lang.voice!r} is not a Kokoro voice")
            elif not lang.voice.startswith(support.voice_prefixes):
                errors.append(f"{where}.voice: {lang.voice!r} does not speak {code!r}")
            if set(lang.measure_descriptions) != set(self.measures):
                errors.append(f"{where}.measure_descriptions must describe exactly {self.measures}")
            if not _mentions(lang.disclaimer, self.plan_name):
                errors.append(f"{where}.disclaimer must name the plan ({self.plan_name!r})")
            if not re.search(rf"(?<!\d){re.escape(self.escalation.opt_out_digit)}(?!\d)",
                             lang.opt_out):  # fmt: skip
                errors.append(
                    f"{where}.opt_out must tell members to press {self.escalation.opt_out_digit}"
                )
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def language_for(self, member_language: str | None) -> str:
        """The member's language if this customer supports it, else the default."""
        return member_language if member_language in self.languages else self.default_language

    def profile(self, language: str) -> OutreachProfile:
        lang = self.languages[language]
        return OutreachProfile(
            plan_name=self.plan_name,
            language=language,
            disclaimer=lang.disclaimer,
            opt_out=lang.opt_out,
            max_words=self.max_words,
            max_gaps_per_script=self.max_gaps_per_script,
            allowed_member_fields=self.allowed_member_fields,
            measure_descriptions={m: lang.measure_descriptions[m] for m in self.measures},
            script_prompt=lang.script_prompt,
            revise_prompt=lang.revise_prompt,
            campaign=self.campaign,
        )


def _mentions(text: str, phrase: str) -> bool:
    from caregap.outreach.compliance import normalize

    return f" {normalize(phrase)} " in f" {normalize(text)} "


class ConfigError(ValueError):
    pass


def _prompt_variables(prompts_dir: Path) -> dict[str, set[str]]:
    from caregap.prompts import read_file

    out: dict[str, set[str]] = {}
    for path in sorted(prompts_dir.glob("*.yaml")):
        name, _, messages = read_file(path)
        out[name] = {v for m in messages for v in re.findall(r"\{\{(\w+)\}\}", m["content"])}
    return out


def load(path: Path) -> CustomerConfig:
    try:
        cfg = CustomerConfig.model_validate(yaml.safe_load(path.read_text()))
    except Exception as exc:
        raise ConfigError(f"{path}: {exc}") from exc
    if cfg.id != path.stem:
        raise ConfigError(f"{path}: id {cfg.id!r} must match the file name")
    return cfg


def load_all(
    directory: Path = DEFAULT_DIR,
    *,
    prompts_dir: Path | None = PROMPTS_DIR,
    pronunciations: Path | None = None,
) -> dict[str, CustomerConfig]:
    """Load and cross-check every config: unique IDs, members belong to one customer,
    referenced prompts exist and use only allowed variables, plan names have a
    pronunciation."""
    configs = [load(p) for p in sorted(directory.glob("*.yaml"))]
    if not configs:
        raise ConfigError(f"no customer configs in {directory}")
    errors: list[str] = []
    for i, a in enumerate(configs):
        for b in configs[i + 1 :]:
            (alo, ahi), (blo, bhi) = a.members.bounds(), b.members.bounds()
            if alo <= bhi and blo <= ahi:
                errors.append(f"{a.id} and {b.id}: member age ranges overlap")
    if prompts_dir is not None:
        variables = _prompt_variables(prompts_dir)
        for c in configs:
            allowed = PROMPT_VARIABLES | set(c.allowed_member_fields)
            for code, lang in c.languages.items():
                for name in (lang.script_prompt, lang.revise_prompt):
                    if name not in variables:
                        errors.append(f"{c.id}.{code}: prompt {name!r} not in {prompts_dir}")
                    elif extra := variables[name] - allowed:
                        errors.append(
                            f"{c.id}.{code}: prompt {name!r} uses {sorted(extra)}, "
                            "which this customer does not allow"
                        )
    from caregap.voice.pronunciation import DEFAULT_PATH, PronunciationDict

    dictionary = PronunciationDict.load(pronunciations or DEFAULT_PATH)
    plans = {t.lower() for t in dictionary.terms("plan")}
    errors += [
        f"{c.id}: plan name {c.plan_name!r} has no entry in the pronunciation dictionary"
        for c in configs
        if c.plan_name.lower() not in plans
    ]
    if errors:
        raise ConfigError("; ".join(errors))
    return {c.id: c for c in configs}


def customer_for(
    configs: dict[str, CustomerConfig], birth_date: date, measurement_year: int
) -> CustomerConfig | None:
    return next(
        (c for c in configs.values() if c.members.includes(birth_date, measurement_year)), None
    )


def main(argv: list[str] | None = None) -> int:
    directory = Path(argv[0]) if argv else DEFAULT_DIR
    try:
        configs = load_all(directory)
    except ConfigError as exc:
        print(f"[customers] invalid: {exc}", file=sys.stderr)
        return 1
    for c in configs.values():
        lo, hi = c.members.bounds()
        print(
            f"[customers] {c.id}: {c.plan_name}, ages {lo}-{hi}, measures {list(c.measures)}, "
            f"languages {list(c.languages)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
