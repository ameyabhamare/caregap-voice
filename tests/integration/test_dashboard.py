"""The dashboard (Phase 10): its queries, and every tab rendered with Streamlit's test
runner against the audit test data (needs `make up`)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from caregap.config import get_settings
from caregap.dashboard import data
from tests.integration.test_audit import CALL_USD, run_member
from tests.integration.test_audit import h as h
from tests.integration.test_campaign import Harness
from tests.integration.test_campaign import db as db

pytestmark = pytest.mark.integration

APP = Path(__file__).resolve().parents[2] / "src/caregap/dashboard/app.py"


def test_dashboard_queries(h: Harness) -> None:
    cid, _ = run_member(h)
    assert data.funnel(h.db, cid)["states"] == {"selected": 3, "escalated": 1, "skipped": 4}
    assert data.funnel(h.db, cid)["calls"] == {"completed": 1}
    assert data.members(h.db, cid)[0] == "m-a"
    [msg] = data.cost_per_message(h.db)
    assert (msg["customer"], msg["kind"], msg["messages"]) == ("sunrise", "outreach script", 1)
    assert msg["total_usd"] == CALL_USD
    stages = {r["stage"]: r for r in data.latency(h.db, "sunrise")}
    assert set(stages) == {"generate", "compliance", "tts", "call"}
    assert stages["generate"]["n"] == 1 and stages["call"]["n"] == 1
    assert data.latency(h.db, "horizon") == []
    assert data.budget(h.db, Decimal("5"))["cap_usd"] == Decimal("5")


def test_dashboard_renders_every_tab(h: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    from streamlit.testing.v1 import AppTest

    run_member(h)
    monkeypatch.setenv("DATABASE_URL", h.db)
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://127.0.0.1:9")  # nothing listens
    get_settings.cache_clear()
    try:
        at = AppTest.from_file(str(APP), default_timeout=60).run()
    finally:
        get_settings.cache_clear()
    assert not at.exception, at.exception
    assert [t.label for t in at.tabs] == ["Campaigns", "Evals", "Cost", "Latency",
                                          "Trace a member"]  # fmt: skip
    markdown = " ".join(m.value for m in at.markdown)
    assert "escalated" in markdown and "awv" in markdown
