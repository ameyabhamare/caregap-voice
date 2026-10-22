import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from caregap.data.fhir_load import (
    BundleSource,
    load_bundles,
    rewrite_to_put,
    synthea_sources,
)


def _entry(rtype: str, rid: str | None, full_url: str, method: str = "POST") -> Any:
    resource: dict[str, Any] = {"resourceType": rtype}
    if rid:
        resource["id"] = rid
    return {"fullUrl": full_url, "resource": resource, "request": {"method": method, "url": rtype}}


def test_rewrite_uses_resource_id_or_fullurl_uuid() -> None:
    bundle = {
        "entry": [
            _entry("Patient", "edge-x", "urn:uuid:edge-x"),
            _entry("Encounter", None, "urn:uuid:aaaa-bbbb"),
        ]
    }
    bundle["entry"][1]["request"]["ifNoneExist"] = "identifier=x|1"
    rewrite_to_put(bundle)
    assert bundle["entry"][0]["request"] == {"method": "PUT", "url": "Patient/edge-x"}
    assert bundle["entry"][1]["request"] == {"method": "PUT", "url": "Encounter/aaaa-bbbb"}
    assert bundle["entry"][1]["resource"]["id"] == "aaaa-bbbb"


def test_rewrite_leaves_non_create_entries_alone() -> None:
    entry = _entry("Patient", "p1", "urn:uuid:p1", method="GET")
    rewrite_to_put({"entry": [entry]})
    assert entry["request"]["method"] == "GET"


def test_rewrite_rejects_entry_without_any_id() -> None:
    with pytest.raises(ValueError, match="no id"):
        rewrite_to_put({"entry": [_entry("Patient", None, "")]})


def test_sources_put_hospitals_first_and_sort_patients(tmp_path: Path) -> None:
    for name in (
        "b_2.json",
        "practitionerInformation1.json",
        "a_1.json",
        "hospitalInformation1.json",
    ):
        (tmp_path / name).write_text("{}")
    shared, patients = synthea_sources(tmp_path)
    assert [s.name for s in shared] == [
        "hospitalInformation1.json",
        "practitionerInformation1.json",
    ]
    assert [s.name for s in patients] == ["a_1.json", "b_2.json"]
    assert all(s.synthea_patient for s in patients)
    assert not any(s.synthea_patient for s in shared)


def _statin_bundle() -> Any:
    pid = "11111111-0000-0000-0000-000000000001"
    order = {
        "resourceType": "MedicationRequest",
        "id": "o1",
        "status": "active",
        "authoredOn": "2015-01-01T00:00:00+00:00",
        "medicationCodeableConcept": {"text": "Simvastatin 20 MG Oral Tablet"},
    }
    return {
        "resourceType": "Bundle",
        "type": "transaction",
        "entry": [
            _entry("Patient", pid, f"urn:uuid:{pid}"),
            {"fullUrl": "urn:uuid:o1", "resource": order, "request": {"method": "POST"}},
        ],
    }


def test_load_adds_fills_only_to_synthea_patients_and_puts_everything() -> None:
    seen: list[Any] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"resourceType": "Bundle"})

    sources = [
        BundleSource("synthea.json", _statin_bundle, synthea_patient=True),
        BundleSource("edge.json", _statin_bundle, synthea_patient=False),
    ]
    stats = load_bundles(
        "http://fhir", sources, fill_seed=1, workers=1, log=lambda _: None,
        transport=httpx.MockTransport(handler),
    )  # fmt: skip
    assert stats.failures == []
    assert stats.fills > 0
    synthea_types = [e["resource"]["resourceType"] for e in seen[0]["entry"]]
    edge_types = [e["resource"]["resourceType"] for e in seen[1]["entry"]]
    assert "MedicationDispense" in synthea_types
    assert "MedicationDispense" not in edge_types
    assert all(e["request"]["method"] == "PUT" for b in seen for e in b["entry"])


def test_load_retries_transient_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("caregap.data.fhir_load.time.sleep", lambda _: None)
    calls = iter([503, 503, 200])

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(next(calls), json={})

    stats = load_bundles(
        "http://fhir", [BundleSource("x.json", _statin_bundle, synthea_patient=False)],
        fill_seed=None, workers=1, log=lambda _: None, transport=httpx.MockTransport(handler),
    )  # fmt: skip
    assert stats.failures == []
    assert stats.bundles == 1


def test_load_reports_client_errors_without_retrying(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("caregap.data.fhir_load.time.sleep", lambda _: None)
    calls = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, text="HAPI-0000: bad reference")

    stats = load_bundles(
        "http://fhir", [BundleSource("x.json", _statin_bundle, synthea_patient=False)],
        fill_seed=None, workers=1, log=lambda _: None, transport=httpx.MockTransport(handler),
    )  # fmt: skip
    assert len(calls) == 1
    assert len(stats.failures) == 1
    assert "bad reference" in stats.failures[0]
