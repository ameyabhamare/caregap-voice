"""Simulated statin pharmacy fills (MedicationDispense) for Synthea members.

Synthea models a statin as a yearly prescription renewal (MedicationRequest) with no
dispense events and no days supply, so adherence cannot be measured from its output.
This module adds deterministic, seeded fills for every member with an active statin
regimen in the fill window, drawn from three adherence profiles. Each fill is tagged
``simulated-fill`` so it is never mistaken for Synthea output.

The same seed and bundle always produce the same fills.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from caregap.data.codes import SYNTHETIC_TAG_SYSTEM, is_statin

FILL_WINDOW_START = date(2024, 1, 1)
FILL_WINDOW_END = date(2025, 12, 31)
# A Synthea statin order is renewed yearly, so a non-active order covers about a year.
ORDER_COVERAGE_DAYS = 365

_NAMESPACE = uuid.UUID("6c0b3f0e-6a7e-4f2b-9a51-2f6a1c7e0d11")

Resource = dict[str, Any]


@dataclass(frozen=True)
class AdherenceProfile:
    name: str
    weight: float
    # Days between running out of one fill and picking up the next.
    min_gap: int
    max_gap: int


PROFILES = (
    AdherenceProfile("adherent", 0.60, 0, 4),
    AdherenceProfile("partial", 0.25, 5, 20),
    AdherenceProfile("poor", 0.15, 15, 45),
)


def _medication_display(order: Resource, medications: dict[str, Resource]) -> str:
    if "medicationCodeableConcept" in order:
        return str(order["medicationCodeableConcept"].get("text", ""))
    ref = order.get("medicationReference", {}).get("reference", "")
    med = medications.get(ref.removeprefix("urn:uuid:"))
    return str(med["code"].get("text", "")) if med else ""


def _medication_concept(order: Resource, medications: dict[str, Resource]) -> Resource:
    if "medicationCodeableConcept" in order:
        concept: Resource = order["medicationCodeableConcept"]
        return concept
    ref = order["medicationReference"]["reference"].removeprefix("urn:uuid:")
    code: Resource = medications[ref]["code"]
    return code


def _authored(order: Resource) -> date:
    return date.fromisoformat(order["authoredOn"][:10])


def regimen_window(orders: list[Resource]) -> tuple[date, date] | None:
    """Days within the fill window when the member is on a statin, or None."""
    if not orders:
        return None
    orders = sorted(orders, key=_authored)
    start = max(_authored(orders[0]), FILL_WINDOW_START)
    last = orders[-1]
    if last.get("status") == "active":
        end = FILL_WINDOW_END
    else:
        end = min(_authored(last) + timedelta(days=ORDER_COVERAGE_DAYS - 1), FILL_WINDOW_END)
    return (start, end) if start <= end else None


def simulate_fills(bundle: Resource, seed: int) -> list[Resource]:
    """MedicationDispense resources for the statin user in this Synthea patient bundle."""
    resources = [e["resource"] for e in bundle.get("entry", [])]
    patients = [r for r in resources if r["resourceType"] == "Patient"]
    if len(patients) != 1:
        return []
    patient_id = patients[0]["id"]
    medications = {r["id"]: r for r in resources if r["resourceType"] == "Medication"}
    orders = [
        r
        for r in resources
        if r["resourceType"] == "MedicationRequest"
        and "authoredOn" in r
        and is_statin(_medication_display(r, medications))
    ]
    window = regimen_window(orders)
    if window is None:
        return []
    start, end = window
    orders.sort(key=_authored)

    rng = random.Random(f"{seed}:{patient_id}")  # noqa: S311 - simulation, not security
    profile = rng.choices(PROFILES, weights=[p.weight for p in PROFILES])[0]
    days_supply = rng.choices((30, 90), weights=(0.7, 0.3))[0]

    fills: list[Resource] = []
    fill_date = start + timedelta(days=rng.randint(0, 7))
    while fill_date <= end:
        order = max(
            (o for o in orders if _authored(o) <= fill_date), key=_authored, default=orders[0]
        )
        fills.append(
            _dispense(patient_id, order, medications, fill_date, days_supply, profile.name)
        )
        fill_date += timedelta(days=days_supply + rng.randint(profile.min_gap, profile.max_gap))
    return fills


def _dispense(
    patient_id: str,
    order: Resource,
    medications: dict[str, Resource],
    fill_date: date,
    days_supply: int,
    profile: str,
) -> Resource:
    return {
        "resourceType": "MedicationDispense",
        "id": str(uuid.uuid5(_NAMESPACE, f"{patient_id}:{fill_date.isoformat()}")),
        "meta": {
            "tag": [
                {"system": SYNTHETIC_TAG_SYSTEM, "code": "simulated-fill"},
                {"system": SYNTHETIC_TAG_SYSTEM, "code": f"adherence-{profile}"},
            ]
        },
        "status": "completed",
        "medicationCodeableConcept": _medication_concept(order, medications),
        "subject": {"reference": f"urn:uuid:{patient_id}"},
        "authorizingPrescription": [{"reference": f"urn:uuid:{order['id']}"}],
        "quantity": {"value": days_supply, "unit": "tablet"},
        "daysSupply": {
            "value": days_supply,
            "unit": "days",
            "system": "http://unitsofmeasure.org",
            "code": "d",
        },
        "whenHandedOver": f"{fill_date.isoformat()}T09:00:00+00:00",
    }


def add_fills(bundle: Resource, seed: int) -> int:
    """Append simulated fills to a Synthea transaction bundle in place. Returns the count."""
    fills = simulate_fills(bundle, seed)
    for fill in fills:
        bundle["entry"].append(
            {
                "fullUrl": f"urn:uuid:{fill['id']}",
                "resource": fill,
                "request": {"method": "POST", "url": "MedicationDispense"},
            }
        )
    return len(fills)
