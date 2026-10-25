# 11. MCP as the integration surface for agents

Date: 2026-10-24. Status: Accepted.

## Context
The brief asks for the core service to be usable from an agent such as Claude Desktop:
inspect a member's care gaps, search the plan's benefits, log an interaction and hand off
to a person. The HTTP API already holds every rule. An agent host should not need custom
client code, and agent access must not widen what leaves the platform.

## Decision
- **The official MCP Python SDK** (`mcp`, pinned exactly) with four tools:
  `get_member_gaps`, `search_benefits`, `log_interaction` and `escalate_to_human`.
  Writing the JSON-RPC protocol by hand was the alternative; the SDK also brings a client
  for the stdio end-to-end test.
- **A thin layer, like n8n.** Each tool is one call to the core service's HTTP API
  (`CORE_URL`). The rules live in `caregap.members`. The member path segment is
  URL-encoded, so a member id cannot reach another endpoint.
- **Stdio only.** Claude Desktop starts `python -m caregap.mcp` as a local process. There
  is no HTTP transport, so no new port and no auth to build. A remote agent would need
  streamable HTTP plus authentication; that is a later decision.
- **Minimum necessary.** `get_member_gaps` shows only the plan's `allowed_member_fields`
  (the list that also limits prompts: Sunrise `given_name`, Horizon nothing) and only the
  plan's measures. No phone numbers, family names or birth dates.
- **Writes are narrow.** `log_interaction` takes a fixed channel and outcome plus a short
  summary (new `member_interactions` table). `escalate_to_human` adds an escalation of a
  new kind, `agent_request`. The agent's reason goes in a new `note` column, never in
  `detail`, so notifications keep carrying IDs only. A repeat with the same reason while
  the request is open returns the same escalation, enforced by a partial unique index.
  The existing outbox and n8n notification workflow deliver it unchanged. No tools to
  start campaigns or edit scripts.
- **Benefits are returned as written.** `search_benefits` returns retrieved lines with
  ids and generates nothing, so the grounding check does not apply. Spoken answers still
  go only through the callback agent and its grounding check. Query embeddings go through
  the metered `EmbeddingClient` (purpose `benefit-search`).

## Consequences
- Whatever the tools return goes to the agent host. For Claude Desktop that means
  Anthropic's API: data leaves the machine. That is acceptable only because every member
  is synthetic. With real members this needs a BAA and a review of which fields cross.
- Gap rows include their reason (for example "no diabetes diagnosis" or a statin PDC).
  That is clinical information an agent sees. It is fine for a care-team tool on
  synthetic data, but it would need review before real use.
- Claude Desktop shows each tool call and asks before running it, so the person stays in
  the loop for writes. The tools still carry the right hints: read-only for the two
  reads, non-destructive for the writes, and idempotent for escalation.
- Everything an agent does is in Postgres (`member_interactions`, `escalations` with
  `kind = 'agent_request'`), ready for the Phase 10 audit view.
