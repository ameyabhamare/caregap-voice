import copy
from datetime import date
from typing import Any

import pytest

from caregap.data.fills import (
    FILL_WINDOW_END,
    FILL_WINDOW_START,
    add_fills,
    regimen_window,
    simulate_fills,
)

PATIENT_ID = "11111111-2222-3333-4444-555555555555"


def _order(oid: str, authored: str, status: str, text: str = "Simvastatin 20 MG Oral Tablet") -> Any:
    return {
        "resourceType": "MedicationRequest",
        "id": oid,
        "status": status,
        "authoredOn": f"{authored}T10:00:00+00:00",
        "medicationCodeableConcept": {"coding": [{"code": "312961"}], "text": text},
    }


def _bundle(*resources: Any) -> Any:
    patient = {"resourceType": "Patient", "id": PATIENT_ID}
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            {"fullUrl": f"urn:uuid:{r['id']}", "resource": r} for r in (patient, *resources)
        ],
    }


def _fill_dates(fills: list[Any]) -> list[date]:
    return [date.fromisoformat(f["whenHandedOver"][:10]) for f in fills]


def test_active_long_running_regimen_fills_whole_window() -> None:
    fills = simulate_fills(_bundle(_order("o1", "2015-03-01", "active")), seed=1)
    dates = _fill_dates(fills)
    assert dates[0] >= FILL_WINDOW_START
    assert dates[-1] <= FILL_WINDOW_END
    assert len(fills) >= 6


def test_same_seed_same_fills_different_seed_differs() -> None:
    b = _bundle(_order("o1", "2015-03-01", "active"))
    assert simulate_fills(b, seed=1) == simulate_fills(copy.deepcopy(b), seed=1)
    runs = {tuple(_fill_dates(simulate_fills(b, seed=s))) for s in range(10)}
    assert len(runs) > 1


def test_non_statin_gets_no_fills() -> None:
    order = _order("o1", "2015-03-01", "active", text="Metformin 500 MG Oral Tablet")
    assert simulate_fills(_bundle(order), seed=1) == []


def test_completed_order_covers_one_year_then_stops() -> None:
    window = regimen_window([_order("o1", "2024-03-01", "completed")])
    assert window == (date(2024, 3, 1), date(2025, 2, 28))


def test_order_after_window_gives_no_regimen() -> None:
    assert regimen_window([_order("o1", "2026-02-01", "active")]) is None


def test_stopped_before_window_gives_no_regimen() -> None:
    assert regimen_window([_order("o1", "2021-01-01", "completed")]) is None


def test_fill_references_latest_order_before_fill() -> None:
    fills = simulate_fills(
        _bundle(_order("o1", "2024-01-01", "completed"), _order("o2", "2025-01-01", "active")),
        seed=3,
    )
    for fill, when in zip(fills, _fill_dates(fills), strict=True):
        expected = "o2" if when >= date(2025, 1, 1) else "o1"
        assert fill["authorizingPrescription"][0]["reference"] == f"urn:uuid:{expected}"


def test_medication_reference_is_resolved() -> None:
    med = {
        "resourceType": "Medication",
        "id": "m1",
        "code": {"coding": [{"code": "617312"}], "text": "atorvastatin 40 MG Oral Tablet"},
    }
    order = _order("o1", "2015-03-01", "active")
    del order["medicationCodeableConcept"]
    order["medicationReference"] = {"reference": "urn:uuid:m1"}
    fills = simulate_fills(_bundle(med, order), seed=1)
    assert fills
    assert fills[0]["medicationCodeableConcept"]["text"] == "atorvastatin 40 MG Oral Tablet"


def test_fills_are_tagged_and_shaped() -> None:
    fills = simulate_fills(_bundle(_order("o1", "2015-03-01", "active")), seed=1)
    ids = {f["id"] for f in fills}
    assert len(ids) == len(fills)
    for f in fills:
        tags = {t["code"] for t in f["meta"]["tag"]}
        assert "simulated-fill" in tags
        assert f["subject"]["reference"] == f"urn:uuid:{PATIENT_ID}"
        assert f["daysSupply"]["value"] in (30, 90)


@pytest.mark.parametrize("seed", range(5))
def test_fills_never_overlap(seed: int) -> None:
    fills = simulate_fills(_bundle(_order("o1", "2015-03-01", "active")), seed=seed)
    dates = _fill_dates(fills)
    for prev, nxt, f in zip(dates, dates[1:], fills, strict=False):
        assert (nxt - prev).days >= f["daysSupply"]["value"]


def test_add_fills_appends_transaction_entries() -> None:
    b = _bundle(_order("o1", "2015-03-01", "active"))
    before = len(b["entry"])
    n = add_fills(b, seed=1)
    assert n > 0
    assert len(b["entry"]) == before + n
    assert b["entry"][-1]["request"] == {"method": "POST", "url": "MedicationDispense"}
