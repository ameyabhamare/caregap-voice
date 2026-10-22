"""Load Synthea and edge-case transaction bundles into HAPI FHIR.

Every ``POST`` (create) entry is rewritten to ``PUT Type/<id>`` using the ID the bundle
already carries (Synthea's UUIDs, edge-case member IDs). HAPI then stores resources
under stable IDs, so a re-run updates in place instead of duplicating, and two runs from
an empty server produce the same export.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any

import httpx

from caregap.data import edge_cases
from caregap.data.fills import add_fills

Resource = dict[str, Any]

FHIR_JSON = "application/fhir+json"
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


def rewrite_to_put(bundle: Resource) -> Resource:
    """Turn create entries into idempotent updates with client-assigned IDs (in place)."""
    for entry in bundle.get("entry", []):
        request = entry.get("request", {})
        if request.get("method") != "POST":
            continue
        resource = entry["resource"]
        rid = resource.get("id") or entry.get("fullUrl", "").removeprefix("urn:uuid:")
        if not rid:
            raise ValueError(f"{resource['resourceType']} entry has no id or urn:uuid fullUrl")
        resource["id"] = rid
        entry["request"] = {"method": "PUT", "url": f"{resource['resourceType']}/{rid}"}
    return bundle


@dataclass(frozen=True)
class BundleSource:
    name: str
    read: Callable[[], Resource]
    synthea_patient: bool


def _read_json(path: Path) -> Resource:
    data: Resource = json.loads(path.read_text())
    return data


def _given(bundle: Resource) -> Resource:
    return bundle


def synthea_sources(fhir_dir: Path) -> tuple[list[BundleSource], list[BundleSource]]:
    """(shared bundles: hospitals and practitioners, patient bundles), in stable order."""
    shared: list[BundleSource] = []
    patients: list[BundleSource] = []
    for path in sorted(fhir_dir.glob("*.json")):
        is_shared = path.name.startswith(("hospitalInformation", "practitionerInformation"))
        src = BundleSource(path.name, partial(_read_json, path), synthea_patient=not is_shared)
        (shared if is_shared else patients).append(src)
    # Hospitals before practitioners: practitioner roles reference organizations.
    shared.sort(key=lambda s: not s.name.startswith("hospital"))
    return shared, patients


def edge_case_sources(path: Path = edge_cases.DEFAULT_PATH) -> list[BundleSource]:
    return [
        BundleSource(f"{mid}.json", partial(_given, bundle), synthea_patient=False)
        for mid, bundle in edge_cases.build_all(path).items()
    ]


@dataclass
class LoadStats:
    bundles: int = 0
    resources: int = 0
    fills: int = 0
    seconds: float = 0.0
    failures: list[str] = field(default_factory=list)


def _post_transaction(client: httpx.Client, bundle: Resource, attempts: int = 4) -> None:
    body = json.dumps(bundle)
    for attempt in range(attempts):
        try:
            r = client.post("", content=body, headers={"Content-Type": FHIR_JSON})
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
        else:
            if r.status_code not in _RETRY_STATUS or attempt == attempts - 1:
                if r.is_error:
                    raise RuntimeError(f"HTTP {r.status_code}: {r.text[:500]}")
                return
        time.sleep(2**attempt)


def load_bundles(
    base_url: str,
    sources: Iterable[BundleSource],
    *,
    fill_seed: int | None,
    workers: int = 4,
    log: Callable[[str], None] = print,
    transport: httpx.BaseTransport | None = None,
) -> LoadStats:
    """Load bundles concurrently. Simulated fills are added to Synthea patient bundles."""
    stats = LoadStats()
    started = time.monotonic()
    sources = list(sources)

    def send(src: BundleSource) -> tuple[int, int, str | None]:
        # Each worker reads its own bundle, so only bundles in flight are held in memory.
        try:
            bundle = src.read()
            fills = 0
            if src.synthea_patient and fill_seed is not None:
                fills = add_fills(bundle, fill_seed)
            _post_transaction(client, rewrite_to_put(bundle))
        except Exception as exc:
            return 0, 0, f"{src.name}: {exc}"
        return len(bundle["entry"]), fills, None

    timeout = httpx.Timeout(600, connect=10)
    with (
        httpx.Client(base_url=base_url, timeout=timeout, transport=transport) as client,
        ThreadPoolExecutor(workers) as pool,
    ):
        for n_res, n_fills, error in pool.map(send, sources):
            stats.bundles += 1
            if error:
                stats.failures.append(error)
                log(f"  FAILED {error[:300]}")
            stats.resources += n_res
            stats.fills += n_fills
            if stats.bundles % 50 == 0 or stats.bundles == len(sources):
                elapsed = time.monotonic() - started
                log(
                    f"  {stats.bundles}/{len(sources)} bundles, {stats.resources} resources, "
                    f"{stats.resources / max(elapsed, 1e-9):.0f} res/s"
                )
    stats.seconds = time.monotonic() - started
    return stats
