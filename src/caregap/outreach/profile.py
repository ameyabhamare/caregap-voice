"""Outreach profile: one customer in one language, as generation and compliance see it.

Built from a customer config (``caregap.customers.CustomerConfig.profile``).
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
    """One customer in one language: what generation and compliance need."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_name: str
    language: str
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
    script_prompt: str
    revise_prompt: str
    campaign: CampaignPolicy
