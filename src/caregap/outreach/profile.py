"""Outreach profile: the per-plan settings generation and compliance depend on.

Phase 3 ships one built-in default. Phase 6 replaces it with validated per-customer YAML
configs of the same shape, so nothing else changes.
"""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CampaignPolicy(BaseModel):
    """When and how often a member may be called, and how many messages a human approves."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    #: Members' local time zone (the synthetic population lives in Massachusetts).
    timezone: str
    call_window_start: time
    call_window_end: time
    #: ISO weekdays, Monday = 1.
    call_days: frozenset[int]
    #: Calls per member per campaign, including the first.
    max_call_attempts: int = Field(ge=1)
    retry_delay_minutes: int = Field(ge=0)
    #: The first N messages of every campaign wait for a human, even when compliant.
    approval_first_n: int = Field(ge=0)

    @model_validator(mode="after")
    def _check(self) -> CampaignPolicy:
        if self.call_window_start >= self.call_window_end:
            raise ValueError("call_window_start must be before call_window_end")
        if not self.call_days or not self.call_days <= set(range(1, 8)):
            raise ValueError("call_days must be ISO weekdays 1-7")
        return self


class OutreachProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_name: str
    #: Must appear verbatim (case, spacing and punctuation-insensitive) in every script.
    disclaimer: str
    #: Must appear verbatim, as above.
    opt_out: str
    max_words: int = Field(gt=0)
    max_gaps_per_script: int = Field(ge=1)
    #: Member fields a script may use. Anything else about the member is PHI to keep out.
    allowed_member_fields: tuple[str, ...]
    #: How each measure is described to the member: services, never diagnoses or drugs.
    measure_descriptions: dict[str, str]
    campaign: CampaignPolicy


DEFAULT_PROFILE = OutreachProfile(
    plan_name="Sunrise Medicare Advantage",
    disclaimer=(
        "This message is from Sunrise Medicare Advantage and is for information only. "
        "It is not medical advice."
    ),
    opt_out="To stop these calls, press 9 or say stop.",
    max_words=120,
    max_gaps_per_script=2,
    allowed_member_fields=("given_name",),
    measure_descriptions={
        "awv": "a yearly wellness visit with your doctor",
        "eye_exam": "a yearly eye exam",
        "statin": "keeping your cholesterol medication refills on schedule",
    },
    campaign=CampaignPolicy(
        timezone="America/New_York",
        # Stricter than the TCPA's 8am-9pm, and no Sunday calls.
        call_window_start=time(9, 0),
        call_window_end=time(20, 0),
        call_days=frozenset(range(1, 7)),
        max_call_attempts=3,
        retry_delay_minutes=240,
        approval_first_n=5,
    ),
)
