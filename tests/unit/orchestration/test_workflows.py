"""n8n holds no business logic: its workflows may only route, retry and call the core
service (or the URLs the core service hands it)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

WORKFLOWS = sorted(Path("n8n/workflows").glob("*.json"))
ALLOWED_NODES = {
    "n8n-nodes-base.scheduleTrigger",
    "n8n-nodes-base.webhook",
    "n8n-nodes-base.errorTrigger",
    "n8n-nodes-base.httpRequest",
    "n8n-nodes-base.splitOut",
    "n8n-nodes-base.switch",
    "n8n-nodes-base.if",
    "n8n-nodes-base.respondToWebhook",
}
# HTTP targets: the core service, or a URL taken from a core response.
ALLOWED_URL = re.compile(
    r"^=\{\{ \$env\.CORE_URL \}\}/|^=\{\{ \$json\.twilio\.url \}\}$|^=\{\{ \$\('Get outbox'\)"
)


def load(path: Path) -> dict[str, Any]:
    wf: dict[str, Any] = json.loads(path.read_text())
    return wf


def test_the_four_workflows_are_exported() -> None:
    assert [p.stem for p in WORKFLOWS] == [
        "campaign-tick",
        "error-alerts",
        "notify-outbox",
        "twilio-events",
    ]


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_only_orchestration_nodes(path: Path) -> None:
    wf = load(path)
    assert {n["type"] for n in wf["nodes"]} <= ALLOWED_NODES
    for n in wf["nodes"]:
        if n["type"] == "n8n-nodes-base.httpRequest":
            assert ALLOWED_URL.match(n["parameters"]["url"]), (n["name"], n["parameters"]["url"])


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.stem)
def test_failures_raise_alerts(path: Path) -> None:
    wf = load(path)
    if wf["id"] != "cgErrorAlerts001":
        assert wf["settings"]["errorWorkflow"] == "cgErrorAlerts001"


def test_every_connection_points_at_a_node() -> None:
    for path in WORKFLOWS:
        wf = load(path)
        names = {n["name"] for n in wf["nodes"]}
        for src, outs in wf["connections"].items():
            assert src in names
            for out in outs["main"]:
                assert {c["node"] for c in out} <= names
