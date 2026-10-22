"""Run Synthea with pinned inputs so the generated population is reproducible.

Same jar, seeds, dates and properties produce byte-identical output.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import httpx

SYNTHEA_VERSION = "4.0.0"
JAR_URL = (
    f"https://github.com/synthetichealth/synthea/releases/download/v{SYNTHEA_VERSION}/"
    "synthea-with-dependencies.jar"
)
JAR_SHA256 = "ed43c20ad40ba5c3bc724503a5af032715fe3c491620b766148e7c2361e6ecc1"


@dataclass(frozen=True)
class SyntheaRun:
    population: int = 2000
    seed: int = 20251
    clinician_seed: int = 20251
    # Synthea computes ages from the reference date and stops simulating at the end date.
    # Both are fixed so output never drifts with the wall clock.
    reference_date: str = "20260101"
    end_date: str = "20260101"
    ages: str = "18-90"

    def args(self) -> list[str]:
        return [
            "-s", str(self.seed),
            "-cs", str(self.clinician_seed),
            "-r", self.reference_date,
            "-e", self.end_date,
            "-p", str(self.population),
            "-a", self.ages,
        ]  # fmt: skip


def jar_path(data_dir: Path) -> Path:
    return data_dir / "cache" / f"synthea-{SYNTHEA_VERSION}.jar"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_jar(data_dir: Path) -> Path:
    """Download the pinned Synthea jar once and verify its checksum."""
    path = jar_path(data_dir)
    if path.exists() and sha256_file(path) == JAR_SHA256:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".part")
    with httpx.stream("GET", JAR_URL, follow_redirects=True, timeout=300) as r:
        r.raise_for_status()
        with tmp.open("wb") as f:
            for chunk in r.iter_bytes():
                f.write(chunk)
    actual = sha256_file(tmp)
    if actual != JAR_SHA256:
        tmp.unlink()
        raise RuntimeError(f"Synthea jar checksum mismatch: {actual} != {JAR_SHA256}")
    tmp.rename(path)
    return path


def output_dir(data_dir: Path) -> Path:
    return data_dir / "synthea" / "fhir"


def generate(data_dir: Path, run: SyntheaRun) -> Path:
    """Generate the population into data/synthea/fhir, replacing any previous output."""
    ensure_jar(data_dir)
    shutil.rmtree(data_dir / "synthea", ignore_errors=True)
    env = {**os.environ, "HOST_UID": str(os.getuid()), "HOST_GID": str(os.getgid())}
    subprocess.run(  # noqa: S603
        ["docker", "compose", "--profile", "tools", "run", "--rm", "synthea", *run.args()],  # noqa: S607
        check=True,
        env=env,
    )
    out = output_dir(data_dir)
    if not any(out.glob("*.json")):
        raise RuntimeError(f"Synthea produced no FHIR bundles in {out}")
    return out
