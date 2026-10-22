"""Outreach profile: the per-plan settings generation and compliance depend on.

Phase 3 ships one built-in default. Phase 6 replaces it with validated per-customer YAML
configs of the same shape, so nothing else changes.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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
)
