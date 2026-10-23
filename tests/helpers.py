"""Shared test fixtures: the committed customer configs."""

from __future__ import annotations

from pathlib import Path

from caregap.customers import CustomerConfig, load

SUNRISE: CustomerConfig = load(Path("customers/sunrise.yaml"))
#: Sunrise in English: the profile most tests use.
P = SUNRISE.profile("en")
