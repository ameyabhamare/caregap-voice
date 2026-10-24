"""The core service as an MCP server: four tools for an agent such as Claude Desktop.

    uv run python -m caregap.mcp        # stdio; CORE_URL points at the core service

A thin layer, like n8n: every tool is one call to the core service's HTTP API, and every
rule (which fields and measures an agent may see, idempotent escalations, IDs-only
notifications) lives in ``caregap.members``. Setup for Claude Desktop: docs/mcp.md.
"""

from __future__ import annotations

import sys
from typing import Any, Literal
from urllib.parse import quote

import httpx
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from caregap.members import Channel, Outcome

INSTRUCTIONS = """\
CareGap Voice: care-gap outreach for synthetic health plan members (all data is synthetic).
Look up a member with get_member_gaps before acting for them. Answer benefit questions
only from search_benefits results and cite the line ids; if nothing matches, say so.
Never give medical advice (medicines, symptoms, results, treatment): remind the member of
the service and suggest their doctor, or use escalate_to_human. Log what happened in a
contact with log_interaction."""

DEFAULT_YEAR = 2025


class CoreClient:
    """The core service's HTTP API; errors become ``ToolError`` for the model to read."""

    def __init__(self, http: httpx.Client) -> None:
        self.http = http

    def call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            r = self.http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ToolError(f"core service unreachable at {self.http.base_url}: {exc}") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise ToolError(f"core service refused ({r.status_code}): {detail}")
        return r.json()


def build_server(core: CoreClient) -> MCPServer:
    server: MCPServer = MCPServer("caregap", instructions=INSTRUCTIONS, version="0.1.0")
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)

    @server.tool(annotations=read_only)
    def get_member_gaps(member_id: str, year: int = DEFAULT_YEAR) -> dict[str, Any]:
        """A member's health plan, preferred contact language, the member fields the plan
        allows an agent to see, and the plan's care gap measures for the measurement year.
        Status "gap" is an open gap; "met", "not_eligible" and "excluded" need no outreach."""
        result: dict[str, Any] = core.call(
            "GET", f"/members/{quote(member_id, safe='')}", params={"year": year}
        )
        return result

    @server.tool(annotations=read_only)
    def search_benefits(customer: str, query: str, k: int = 5) -> dict[str, Any]:
        """Search a plan's Summary of Benefits. Returns benefit lines exactly as written,
        nearest first, each with an id to cite. customer is the plan id from
        get_member_gaps (e.g. "sunrise" or "horizon"). Quote amounts only from these lines."""
        hits = core.call("POST", "/benefits/search",
                         json={"customer": customer, "query": query, "k": k})  # fmt: skip
        return {"customer": customer, "results": hits}

    @server.tool(annotations=ToolAnnotations(destructive_hint=False, open_world_hint=False))
    def log_interaction(
        member_id: str, channel: Channel, outcome: Outcome, summary: str
    ) -> dict[str, Any]:
        """Record a contact with a member: the channel, how it ended, and a short factual
        summary (no medical details). Returns the interaction id."""
        result: dict[str, Any] = core.call(
            "POST", f"/members/{quote(member_id, safe='')}/interactions",
            json={"channel": channel, "outcome": outcome, "summary": summary, "source": "mcp"},
        )  # fmt: skip
        return result

    notifies = ToolAnnotations(destructive_hint=False, idempotent_hint=True, open_world_hint=True)

    @server.tool(annotations=notifies)
    def escalate_to_human(
        member_id: str, reason: str, campaign_id: int | None = None
    ) -> dict[str, Any]:
        """Ask a person on the care team to follow up with a member, e.g. for a medical
        question or a request to speak to someone. The team is notified with IDs only.
        Asking again with the same reason while the request is open returns the same one."""
        result: dict[str, Any] = core.call(
            "POST", "/escalations",
            json={"member_id": member_id, "reason": reason, "campaign_id": campaign_id,
                  "source": "mcp"},
        )  # fmt: skip
        return result

    return server


def main(transport: Literal["stdio"] = "stdio") -> None:
    from caregap.config import get_settings

    url = get_settings().core_url
    with httpx.Client(base_url=url, timeout=60.0) as http:
        print(f"[caregap.mcp] core service at {url}", file=sys.stderr)  # stdout is the protocol
        build_server(CoreClient(http)).run(transport)
