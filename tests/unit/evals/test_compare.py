"""Claude vs local comparison: the cost projection and the report (no model calls)."""

from __future__ import annotations

from decimal import Decimal

import pytest

from caregap.config import Settings
from caregap.evals import compare


def test_projection_scales_the_precheck_to_the_full_run() -> None:
    p = compare.Projection(Decimal("0.006"), Decimal("0.01"), scripts=230, questions=88)
    assert p.total == Decimal("2.260")


def test_report_puts_both_sides_and_the_cost_side_by_side() -> None:
    md = compare.comparison_markdown(
        "claude-sonnet-5-5", "ollama:qwen2.5:3b",
        {"sunrise": ({"final_pass_rate": 0.99, "latency_p50_s": 31.0},
                     {"final_pass_rate": 1.0, "latency_p50_s": 4.2})},
        {"horizon": ({"hallucination_rate": 0.0}, {"hallucination_rate": 0.0})},
        {"scripts sunrise": Decimal("0.60"), "callback horizon": Decimal("0.40")},
        {"scripts sunrise": 100, "callback horizon": 40},
    )  # fmt: skip
    assert "| Compliant after one revision | 99.0% | 100.0% |" in md
    assert "| Latency per script, p50 | 31.0 s | 4.2 s |" in md
    assert "| Compliant on the first attempt | - | - |" in md  # missing on both sides
    assert "| Spoken hallucinations | 0.0% | 0.0% |" in md
    assert "| scripts sunrise: 100 messages, $0.0060 each | $0.60 | $0.00 |" in md
    assert "| **Total** (140 messages) | **$1.00** | **$0.00** |" in md


def test_refuses_without_an_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    from caregap import config

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        config, "get_settings", lambda: Settings(_env_file=None, anthropic_api_key=None)
    )
    with pytest.raises(SystemExit, match="ANTHROPIC_API_KEY is not set"):
        compare.main([])
