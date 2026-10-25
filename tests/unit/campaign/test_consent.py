from __future__ import annotations

from collections import Counter

from caregap.campaign.consent import synthetic_consent


def test_synthetic_consent_is_deterministic_and_roughly_90_5_5() -> None:
    ids = [f"member-{i}" for i in range(4000)]
    first = [synthetic_consent(m) for m in ids]
    assert first == [synthetic_consent(m) for m in ids]
    counts = Counter(first)
    assert 0.88 < counts["granted"] / len(ids) < 0.92
    assert 0.04 < counts["revoked"] / len(ids) < 0.06
    assert 0.04 < counts[None] / len(ids) < 0.06
