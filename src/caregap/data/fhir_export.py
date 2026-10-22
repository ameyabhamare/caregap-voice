"""FHIR Bulk Data export ($export) from HAPI into one NDJSON file per resource type."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path

import httpx

# Every type the loader writes. System-level export also covers non-patient types.
EXPORT_TYPES = (
    "Patient",
    "Encounter",
    "Condition",
    "Procedure",
    "Observation",
    "MedicationRequest",
    "MedicationDispense",
    "Medication",
    "Organization",
    "Practitioner",
    "PractitionerRole",
    "Location",
)


class ExportError(RuntimeError):
    pass


def bulk_export(
    base_url: str,
    out_dir: Path,
    *,
    types: tuple[str, ...] = EXPORT_TYPES,
    poll_seconds: float = 10,
    timeout_seconds: float = 3 * 3600,
    log: Callable[[str], None] = print,
) -> dict[str, Path]:
    """Run a system-level export and write ``<out_dir>/<Type>.ndjson``. Returns the files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.ndjson"):
        old.unlink()

    with httpx.Client(timeout=httpx.Timeout(300, connect=10)) as client:
        kick = client.get(
            f"{base_url}/$export",
            params={"_type": ",".join(types)},
            headers={"Accept": "application/fhir+json", "Prefer": "respond-async"},
        )
        if kick.status_code != 202 or "Content-Location" not in kick.headers:
            raise ExportError(f"$export kick-off failed: HTTP {kick.status_code} {kick.text[:300]}")
        status_url = kick.headers["Content-Location"]
        log(f"  export started: {status_url}")

        deadline = time.monotonic() + timeout_seconds
        last_progress = ""
        while True:
            r = client.get(status_url, headers={"Accept": "application/json"})
            if r.status_code == 200:
                manifest = r.json()
                break
            if r.status_code != 202:
                raise ExportError(f"export status failed: HTTP {r.status_code} {r.text[:300]}")
            if time.monotonic() > deadline:
                raise ExportError(f"export did not finish within {timeout_seconds:.0f}s")
            progress = r.headers.get("X-Progress", "in progress")
            if progress != last_progress:
                log(f"  {progress}")
                last_progress = progress
            time.sleep(poll_seconds)

        if manifest.get("error"):
            raise ExportError(f"export reported errors: {manifest['error']}")

        files: dict[str, Path] = {}
        for output in manifest.get("output", []):
            path = out_dir / f"{output['type']}.ndjson"
            # A type can be split across several output files; append them in order.
            with (
                client.stream(
                    "GET", output["url"], headers={"Accept": "application/fhir+ndjson"}
                ) as resp,
                path.open("ab") as f,
            ):
                resp.raise_for_status()
                last = b"\n"
                for chunk in resp.iter_bytes():
                    f.write(chunk)
                    last = chunk[-1:] or last
                if last != b"\n":
                    f.write(b"\n")
            files[output["type"]] = path
        # Free the server-side export binaries.
        client.delete(status_url)
    log(f"  exported {len(files)} resource types to {out_dir}")
    return files
