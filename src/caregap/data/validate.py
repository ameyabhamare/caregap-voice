"""Validate a bulk export and write ``manifest.json``.

The manifest's ``content_digest`` ignores server-assigned metadata (``versionId``,
``lastUpdated``, ``source``) and line order, so two runs from an empty server with the
same inputs must produce the same digest. That is the reproducibility check.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from caregap.data import edge_cases

Resource = dict[str, Any]

REQUIRED_TYPES = (
    "Patient",
    "Encounter",
    "Condition",
    "Procedure",
    "Observation",
    "MedicationRequest",
    "MedicationDispense",
)
_VOLATILE_META = ("versionId", "lastUpdated", "source")


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    counts: dict[str, int] = field(default_factory=dict)
    digests: dict[str, str] = field(default_factory=dict)
    content_digest: str = ""
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))


def canonical(resource: Resource) -> bytes:
    """Stable bytes for a resource, without server-assigned metadata."""
    meta = {k: v for k, v in resource.get("meta", {}).items() if k not in _VOLATILE_META}
    stripped = {k: v for k, v in resource.items() if k != "meta"}
    if meta:
        stripped["meta"] = meta
    return json.dumps(stripped, sort_keys=True, separators=(",", ":")).encode()


def iter_references(obj: Any) -> Iterator[str]:
    if isinstance(obj, dict):
        ref = obj.get("reference")
        if isinstance(ref, str):
            yield ref
        for v in obj.values():
            yield from iter_references(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_references(v)


def validate_export(
    export_dir: Path,
    *,
    expected_patients: int | None = None,
    fhir_base_url: str | None = None,
    edge_case_path: Path = edge_cases.DEFAULT_PATH,
) -> Report:
    report = Report()
    ids: dict[str, set[str]] = defaultdict(set)
    refs: list[tuple[str, str]] = []  # (source type, reference)
    bad_lines: list[str] = []
    type_digests: dict[str, int] = defaultdict(int)

    files = sorted(export_dir.glob("*.ndjson"))
    for path in files:
        file_type = path.stem
        with path.open() as f:
            for n, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    resource = json.loads(line)
                except json.JSONDecodeError as exc:
                    bad_lines.append(f"{path.name}:{n} invalid JSON ({exc.msg})")
                    continue
                rtype, rid = resource.get("resourceType"), resource.get("id")
                if rtype != file_type or not rid:
                    bad_lines.append(f"{path.name}:{n} resourceType={rtype} id={rid}")
                    continue
                if rid in ids[rtype]:
                    bad_lines.append(f"{path.name}:{n} duplicate id {rid}")
                ids[rtype].add(rid)
                # Order-independent digest: sum of per-resource hashes.
                h = int.from_bytes(hashlib.sha256(canonical(resource)).digest(), "big")
                type_digests[rtype] = (type_digests[rtype] + h) % (1 << 256)
                refs.extend((rtype, r) for r in iter_references(resource))

    report.counts = {t: len(v) for t, v in sorted(ids.items())}
    report.digests = {t: f"{d:064x}" for t, d in sorted(type_digests.items())}
    report.content_digest = hashlib.sha256(
        "\n".join(f"{t}:{d}" for t, d in report.digests.items()).encode()
    ).hexdigest()

    report.check("files present", bool(files), f"{len(files)} NDJSON files")
    report.check("lines parse and match their file type", not bad_lines, "; ".join(bad_lines[:5]))
    missing = [t for t in REQUIRED_TYPES if not report.counts.get(t)]
    report.check("required resource types present", not missing, f"missing: {missing}")

    dangling: Counter[str] = Counter()
    for src, ref in refs:
        if ref.startswith("#"):
            continue  # contained resource
        target_type, _, target_id = ref.partition("/")
        if not target_id or "?" in ref or ref.startswith("urn:"):
            dangling[f"{src} -> unresolved {ref[:40]}"] += 1
        elif target_id not in ids.get(target_type, ()):
            dangling[f"{src} -> {target_type}"] += 1
    report.check(
        "every reference resolves within the export",
        not dangling,
        ", ".join(f"{k} x{v}" for k, v in dangling.most_common(5)),
    )

    edge = edge_cases.build_all(edge_case_path)
    missing_edge = sorted(
        f"{e['resource']['resourceType']}/{e['resource']['id']}"
        for bundle in edge.values()
        for e in bundle["entry"]
        if e["resource"]["id"] not in ids.get(e["resource"]["resourceType"], ())
    )
    report.check(
        f"all {len(edge)} edge-case members and their resources present",
        not missing_edge,
        f"{len(missing_edge)} missing, e.g. {missing_edge[:3]}",
    )

    if expected_patients is not None:
        actual = report.counts.get("Patient", 0)
        want = expected_patients + len(edge)
        report.check(
            "patient count = Synthea population + edge cases",
            actual == want,
            f"{actual} exported, expected {want}",
        )

    if fhir_base_url:
        report.checks.append(_compare_with_server(fhir_base_url, report.counts))
    return report


def _compare_with_server(base_url: str, counts: dict[str, int]) -> Check:
    mismatches = []
    with httpx.Client(base_url=base_url, timeout=120) as client:
        for rtype, n in counts.items():
            total = client.get(f"/{rtype}", params={"_summary": "count"}).json()["total"]
            if total != n:
                mismatches.append(f"{rtype}: server {total}, export {n}")
    return Check("export counts match server counts", not mismatches, "; ".join(mismatches))


def write_manifest(report: Report, path: Path, extra: dict[str, Any]) -> None:
    data = {
        **extra,
        "ok": report.ok,
        "counts": report.counts,
        "content_digest": report.content_digest,
        "digests": report.digests,
        "checks": [asdict(c) for c in report.checks],
    }
    path.write_text(json.dumps(data, indent=2) + "\n")


def print_report(report: Report, log: Callable[[str], None] = print) -> None:
    for c in report.checks:
        detail = f": {c.detail}" if c.detail and not c.ok else ""
        log(f"  [{'PASS' if c.ok else 'FAIL'}] {c.name}{detail}")
    total = sum(report.counts.values())
    log(f"  {total} resources across {len(report.counts)} types")
    log(f"  content digest {report.content_digest}")


def compare_manifests(a: Path, b: Path) -> list[str]:
    """Differences in counts or content digests between two manifests (empty if identical)."""
    ma, mb = json.loads(a.read_text()), json.loads(b.read_text())
    diffs = [
        f"{t}: count {ma['counts'].get(t)} vs {mb['counts'].get(t)}, digest differs"
        for t in sorted(set(ma["digests"]) | set(mb["digests"]))
        if ma["digests"].get(t) != mb["digests"].get(t)
    ]
    if ma["content_digest"] != mb["content_digest"] and not diffs:
        diffs.append("content digest differs")
    return diffs


if __name__ == "__main__":
    import sys

    differences = compare_manifests(Path(sys.argv[1]), Path(sys.argv[2]))
    for d in differences:
        print(f"  DIFF {d}")
    print("  runs are identical" if not differences else f"  {len(differences)} differences")
    sys.exit(1 if differences else 0)
