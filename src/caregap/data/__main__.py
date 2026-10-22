"""Seed pipeline CLI: ``python -m caregap.data {generate,load,export,validate,seed}``.

``seed`` runs all four steps. Each step can also be run alone; ``load`` and ``validate``
read the run parameters ``generate`` recorded in ``data/synthea/run.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from caregap.config import get_settings
from caregap.data import edge_cases, synthea
from caregap.data.fhir_export import bulk_export
from caregap.data.fhir_load import edge_case_sources, load_bundles, synthea_sources
from caregap.data.validate import print_report, validate_export, write_manifest


def _run_file(data_dir: Path) -> Path:
    return data_dir / "synthea" / "run.json"


def _read_run(data_dir: Path) -> synthea.SyntheaRun:
    path = _run_file(data_dir)
    if not path.exists():
        sys.exit(f"{path} not found; run the generate step first")
    return synthea.SyntheaRun(**json.loads(path.read_text()))


def generate(data_dir: Path, population: int) -> None:
    run = synthea.SyntheaRun(population=population)
    print(f"[generate] Synthea {synthea.SYNTHEA_VERSION}, {population} members, seed {run.seed}")
    synthea.generate(data_dir, run)
    _run_file(data_dir).write_text(json.dumps(asdict(run), indent=2) + "\n")


def load(data_dir: Path, base_url: str, workers: int) -> None:
    run = _read_run(data_dir)
    shared, patients = synthea_sources(synthea.output_dir(data_dir))
    print(f"[load] {len(shared)} shared + {len(patients)} patient bundles into {base_url}")
    failures: list[str] = []
    for label, sources, fill_seed, n in (
        ("hospitals and practitioners", shared, None, 1),
        ("Synthea members (+ simulated statin fills)", patients, run.seed, workers),
        ("edge-case members", edge_case_sources(), None, workers),
    ):
        print(f"[load] {label}")
        stats = load_bundles(base_url, sources, fill_seed=fill_seed, workers=n)
        print(
            f"[load]   {stats.resources} resources ({stats.fills} simulated fills) "
            f"in {stats.seconds:.0f}s"
        )
        failures += stats.failures
    if failures:
        sys.exit(f"[load] {len(failures)} bundles failed, first: {failures[0][:500]}")


def export(data_dir: Path, base_url: str) -> None:
    print(f"[export] bulk export from {base_url}")
    bulk_export(base_url, data_dir / "export")


def validate(data_dir: Path, base_url: str) -> None:
    run = _read_run(data_dir)
    print("[validate]")
    report = validate_export(
        data_dir / "export", expected_patients=run.population, fhir_base_url=base_url
    )
    print_report(report)
    extra: dict[str, Any] = {
        "synthea_version": synthea.SYNTHEA_VERSION,
        "synthea_run": asdict(run),
        "fill_seed": run.seed,
        "edge_cases_sha256": synthea.sha256_file(edge_cases.DEFAULT_PATH),
    }
    write_manifest(report, data_dir / "export" / "manifest.json", extra)
    if not report.ok:
        sys.exit("[validate] FAILED")


def main(argv: list[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m caregap.data")
    parser.add_argument("step", choices=["generate", "load", "export", "validate", "seed"])
    parser.add_argument("--population", type=int, default=synthea.SyntheaRun.population)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    data_dir, url = settings.data_dir, settings.fhir_base_url

    started = time.monotonic()
    if args.step in ("generate", "seed"):
        generate(data_dir, args.population)
    if args.step in ("load", "seed"):
        load(data_dir, url, args.workers)
    if args.step in ("export", "seed"):
        export(data_dir, url)
    if args.step in ("validate", "seed"):
        validate(data_dir, url)
    print(f"[{args.step}] done in {time.monotonic() - started:.0f}s")


if __name__ == "__main__":
    main()
