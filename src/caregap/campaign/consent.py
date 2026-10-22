"""Synthetic contact consent.

Synthea has no consent records, so every member gets a deterministic synthetic one
(seeded from a hash of the member ID): 90% granted, 5% revoked, 5% unknown (no row).
Unknown means no call. Existing rows are never overwritten, so opt-outs recorded during
campaigns survive a re-seed.

    python -m caregap.campaign.consent      # make seed-consent
"""

from __future__ import annotations

import hashlib
import sys
from typing import Literal

import psycopg

from caregap.config import get_settings

SOURCE = "synthetic-seed"


def synthetic_consent(member_id: str) -> Literal["granted", "revoked"] | None:
    bucket = int.from_bytes(hashlib.sha256(f"consent:{member_id}".encode()).digest()[:4]) % 100
    if bucket < 90:
        return "granted"
    return "revoked" if bucket < 95 else None


def seed_consent(conninfo: str) -> dict[str, int]:
    """Insert synthetic voice consent for members without a consent row."""
    counts = {"granted": 0, "revoked": 0, "unknown": 0, "kept": 0}
    with psycopg.connect(conninfo) as conn:
        members = [r[0] for r in conn.execute("SELECT member_id FROM members ORDER BY member_id")]
        existing = {
            r[0] for r in conn.execute("SELECT member_id FROM consent WHERE channel = 'voice'")
        }
        rows = []
        for member_id in members:
            if member_id in existing:
                counts["kept"] += 1
                continue
            status = synthetic_consent(member_id)
            counts[status or "unknown"] += 1
            if status:
                rows.append((member_id, status))
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO consent (member_id, channel, status, source)"
                " VALUES (%s, 'voice', %s, %s) ON CONFLICT DO NOTHING",
                [(m, s, SOURCE) for m, s in rows],
            )
    return counts


def main() -> int:
    counts = seed_consent(get_settings().database_url)
    print(
        f"[consent] granted {counts['granted']}, revoked {counts['revoked']}, "
        f"unknown {counts['unknown']}, existing kept {counts['kept']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
