import json
import random
from pathlib import Path
from typing import Any

import pytest

from caregap.data.edge_cases import build_all
from caregap.data.validate import canonical, compare_manifests, validate_export, write_manifest


def _write(path: Path, resources: list[Any]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in resources))


def _edge_resources() -> dict[str, list[Any]]:
    """Every edge-case resource, grouped by type, with references rewritten to Type/id."""
    by_type: dict[str, list[Any]] = {}
    for bundle in build_all().values():
        ids = {e["fullUrl"]: f"{e['resource']['resourceType']}/{e['resource']['id']}"
               for e in bundle["entry"]}  # fmt: skip
        for e in bundle["entry"]:
            text = json.dumps(e["resource"])
            for urn, rel in ids.items():
                text = text.replace(f'"{urn}"', f'"{rel}"')
            r = json.loads(text)
            r["meta"] = {**r["meta"], "versionId": "1", "lastUpdated": "2026-10-22T00:00:00Z"}
            by_type.setdefault(r["resourceType"], []).append(r)
    return by_type


@pytest.fixture
def export_dir(tmp_path: Path) -> Path:
    for rtype, resources in _edge_resources().items():
        _write(tmp_path / f"{rtype}.ndjson", resources)
    return tmp_path


def _failed(report: Any) -> set[str]:
    return {c.name for c in report.checks if not c.ok}


def test_edge_case_only_export_passes_structural_checks(export_dir: Path) -> None:
    report = validate_export(export_dir, expected_patients=0)
    assert _failed(report) == set(), [c for c in report.checks if not c.ok]
    assert report.counts["Patient"] == 23


def test_digest_ignores_line_order_and_server_metadata(export_dir: Path) -> None:
    before = validate_export(export_dir).content_digest
    for path in export_dir.glob("*.ndjson"):
        lines = path.read_text().splitlines()
        random.Random(0).shuffle(lines)
        resources = [json.loads(line) for line in lines]
        for r in resources:
            r["meta"]["versionId"] = "7"
            r["meta"]["lastUpdated"] = "2030-01-01T00:00:00Z"
        _write(path, resources)
    assert validate_export(export_dir).content_digest == before


def test_digest_changes_when_content_changes(export_dir: Path) -> None:
    before = validate_export(export_dir).content_digest
    path = export_dir / "Patient.ndjson"
    resources = [json.loads(line) for line in path.read_text().splitlines()]
    resources[0]["gender"] = "unknown"
    _write(path, resources)
    assert validate_export(export_dir).content_digest != before


def test_canonical_keeps_tags_but_drops_volatile_meta() -> None:
    r = {"resourceType": "Patient", "id": "p", "meta": {"versionId": "3", "tag": [{"code": "x"}]}}
    assert json.loads(canonical(r)) == {
        "resourceType": "Patient", "id": "p", "meta": {"tag": [{"code": "x"}]},
    }  # fmt: skip


def test_detects_bad_lines(export_dir: Path) -> None:
    with (export_dir / "Patient.ndjson").open("a") as f:
        f.write("{not json\n")
        f.write(json.dumps({"resourceType": "Encounter", "id": "wrong-file"}) + "\n")
        f.write(json.dumps({"resourceType": "Patient", "id": "edge-awv-met"}) + "\n")
    report = validate_export(export_dir)
    check = next(c for c in report.checks if c.name.startswith("lines parse"))
    assert not check.ok
    assert "invalid JSON" in check.detail
    assert "resourceType=Encounter" in check.detail or "duplicate" in check.detail


def test_detects_dangling_and_unresolved_references(export_dir: Path) -> None:
    with (export_dir / "Encounter.ndjson").open("a") as f:
        f.write(json.dumps({"resourceType": "Encounter", "id": "e-x",
                            "subject": {"reference": "Patient/nobody"}}) + "\n")  # fmt: skip
        f.write(json.dumps({"resourceType": "Encounter", "id": "e-y",
                            "subject": {"reference": "urn:uuid:abc"}}) + "\n")  # fmt: skip
    report = validate_export(export_dir)
    check = next(c for c in report.checks if c.name.startswith("every reference"))
    assert not check.ok
    assert "Encounter -> Patient" in check.detail
    assert "unresolved urn:uuid:abc" in check.detail


def test_detects_missing_edge_case_and_required_type(export_dir: Path) -> None:
    (export_dir / "MedicationDispense.ndjson").unlink()
    report = validate_export(export_dir, expected_patients=5)
    failed = _failed(report)
    assert "required resource types present" in failed
    assert any(name.startswith("all 23 edge-case") for name in failed)
    assert "patient count = Synthea population + edge cases" in failed


def test_manifest_round_trip_and_compare(export_dir: Path, tmp_path: Path) -> None:
    report = validate_export(export_dir)
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    write_manifest(report, a, {"run": 1})
    write_manifest(report, b, {"run": 2})
    assert compare_manifests(a, b) == []
    data = json.loads(b.read_text())
    data["digests"]["Patient"] = "0" * 64
    data["content_digest"] = "x"
    b.write_text(json.dumps(data))
    assert compare_manifests(a, b) == ["Patient: count 23 vs 23, digest differs"]
