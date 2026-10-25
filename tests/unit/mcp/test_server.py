"""The MCP server's tools against a fake core service (in-process MCP client)."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from mcp import Client
from mcp.types import CallToolResult, TextContent

from caregap.mcp.server import CoreClient, build_server

TOOLS = {"get_member_gaps", "search_benefits", "log_interaction", "escalate_to_human"}


class FakeCore:
    """Records requests; answers with ``responses[(method, path)]`` or 200 echo."""

    def __init__(self, responses: dict[tuple[str, str], httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.responses = responses or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = (request.method, request.url.raw_path.decode().split("?")[0])
        return self.responses.get(key, httpx.Response(200, json={"ok": True}))

    def client(self) -> CoreClient:
        return CoreClient(httpx.Client(base_url="http://core", transport=httpx.MockTransport(self)))


def call(core: FakeCore, tool: str, args: dict[str, Any]) -> CallToolResult:
    async def run() -> CallToolResult:
        async with Client(build_server(core.client())) as c:
            return await c.call_tool(tool, args)

    return asyncio.run(run())


def text(result: CallToolResult) -> str:
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


def test_exposes_exactly_the_four_tools_with_honest_annotations() -> None:
    async def run() -> dict[str, Any]:
        async with Client(build_server(FakeCore().client())) as c:
            return {t.name: t for t in (await c.list_tools()).tools}

    tools = asyncio.run(run())
    assert set(tools) == TOOLS
    for name in ("get_member_gaps", "search_benefits"):
        assert tools[name].annotations is not None and tools[name].annotations.read_only_hint
    for name in ("log_interaction", "escalate_to_human"):
        ann = tools[name].annotations
        assert ann is not None and not ann.read_only_hint and ann.destructive_hint is False
    schema = tools["log_interaction"].input_schema["properties"]
    assert schema["channel"]["enum"] == ["phone", "sms", "email", "chat", "other"]
    assert tools["escalate_to_human"].input_schema["required"] == ["member_id", "reason"]


def test_each_tool_is_one_core_call() -> None:
    core = FakeCore()
    call(core, "get_member_gaps", {"member_id": "m-1", "year": 2024})
    call(core, "search_benefits", {"customer": "sunrise", "query": "dental?"})
    call(core, "log_interaction", {"member_id": "m-1", "channel": "phone",
                                   "outcome": "reached", "summary": "Booked a visit."})  # fmt: skip
    call(core, "escalate_to_human", {"member_id": "m-1", "reason": "Wants a nurse."})
    got = [(r.method, r.url.path, r.url.params.get("year"),
            json.loads(r.content) if r.content else None) for r in core.requests]  # fmt: skip
    assert got == [
        ("GET", "/members/m-1", "2024", None),
        ("POST", "/benefits/search", None, {"customer": "sunrise", "query": "dental?", "k": 5}),
        ("POST", "/members/m-1/interactions", None,
         {"channel": "phone", "outcome": "reached", "summary": "Booked a visit.",
          "source": "mcp"}),
        ("POST", "/escalations", None,
         {"member_id": "m-1", "reason": "Wants a nurse.", "campaign_id": None, "source": "mcp"}),
    ]  # fmt: skip


def test_member_ids_cannot_reach_other_endpoints() -> None:
    core = FakeCore()
    call(core, "get_member_gaps", {"member_id": "../escalations"})
    assert core.requests[0].url.raw_path.decode().startswith("/members/..%2Fescalations")


def test_core_refusals_reach_the_model_as_tool_errors() -> None:
    core = FakeCore({("GET", "/members/nobody"): httpx.Response(
        404, json={"detail": "member nobody not found"})})  # fmt: skip
    result = call(core, "get_member_gaps", {"member_id": "nobody"})
    assert result.is_error
    assert text(result).endswith("core service refused (404): member nobody not found")


def test_unreachable_core_is_a_tool_error() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    server = build_server(
        CoreClient(httpx.Client(base_url="http://core", transport=httpx.MockTransport(down)))
    )

    async def run() -> CallToolResult:
        async with Client(server) as c:
            return await c.call_tool("escalate_to_human", {"member_id": "m-1", "reason": "x"})

    result = asyncio.run(run())
    assert result.is_error and "core service unreachable" in text(result)


def test_invalid_arguments_never_reach_the_core() -> None:
    core = FakeCore()
    result = call(core, "log_interaction", {"member_id": "m-1", "channel": "fax",
                                            "outcome": "reached", "summary": "x"})  # fmt: skip
    assert result.is_error and core.requests == []


def test_desktop_config_uses_absolute_paths() -> None:
    from pathlib import Path

    from caregap.mcp.desktop_config import entry

    e = entry("/usr/bin/uv", Path("/repo"), "http://localhost:8000")["mcpServers"]["caregap"]
    assert e["command"] == "/usr/bin/uv"
    assert e["args"] == ["--directory", "/repo", "run", "python", "-m", "caregap.mcp"]
    assert e["env"] == {"CORE_URL": "http://localhost:8000"}
